"""Flow engine shared by the GraphQL API and the Dash dashboard.

Two things live here that used to be tangled into app.py's resolvers.

The first is the stage table. Every stage is one Makefile target plus the
artefacts and summary it is expected to leave behind, declared in one place.
The Makefile stays the single source of truth for *how* a stage runs -- so
`make pnr` by hand and the dashboard's Place & Route button cannot drift --
and this module is the single source of truth for what the flow *is*.

The second is the job runner. Place and route takes minutes. Driven straight
from a request handler it holds the connection open for the duration, and the
browser gives up long before OpenROAD does, which looked exactly like a
crashed tool. So a stage runs on a worker thread, appends to a live log, and
the caller polls. That is also what makes "Generate All" possible: it is just
a job with a list of stages, reporting each one as it finishes.
"""
import glob
import itertools
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths and limits
# ---------------------------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(PROJECT_DIR, "src")
TB_DIR = os.path.join(PROJECT_DIR, "tb")
RESULTS_DIR = os.path.join(PROJECT_DIR, "results")
PNR_DIR = os.path.join(RESULTS_DIR, "pnr")
REPORT_DIR = os.path.join(RESULTS_DIR, "report")
OPTIMIZE_DIR = os.path.join(RESULTS_DIR, "optimize")
CONSTRAINTS_DIR = os.path.join(PROJECT_DIR, "constraints")

NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Extensions accepted on upload, mapped to where the flow expects them.
RTL_EXTENSIONS = (".sv", ".v", ".svh", ".vh")
UPLOAD_DESTINATIONS = {
    ".sv": SRC_DIR, ".v": SRC_DIR, ".svh": SRC_DIR, ".vh": SRC_DIR,
    ".py": TB_DIR, ".sdc": CONSTRAINTS_DIR,
}

MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# GTK/XDG variables that VS Code's Snap packaging leaks into its integrated
# terminal, which make gtkwave load mismatched Snap-bundled GTK modules and
# die with a symbol lookup error. Cleared before every gtkwave launch, the
# same fix the Makefile applies to its `wave` target.
GTKWAVE_CLEAN_ENV_VARS = (
    "GTK_EXE_PREFIX", "GTK_PATH", "GIO_MODULE_DIR", "GTK_IM_MODULE_FILE",
    "GDK_PIXBUF_MODULE_FILE", "XDG_DATA_DIRS", "XDG_DATA_HOME",
    "GSETTINGS_SCHEMA_DIR", "LOCPATH",
)


class FlowError(Exception):
    """A stage could not be started -- bad input, or a missing prerequisite."""


# ---------------------------------------------------------------------------
# Stage table
# ---------------------------------------------------------------------------

class Stage:
    """One Makefile target, plus what it is expected to produce.

    `summary` and `artifact` are templates taking {design}; they are resolved
    against whichever design is current when the stage runs.
    """

    def __init__(self, key, label, target, timeout, hint="",
                 summary=None, artifact=None, image=None, needs=(),
                 python_target=None, python_artifact=None):
        self.key = key
        self.label = label
        self.target = target
        self.timeout = timeout
        self.hint = hint
        self.summary = summary
        self.artifact = artifact
        self.image = image
        self.needs = needs
        # Two stages differ by testbench language: `tb` writes either a
        # SystemVerilog or a cocotb harness, and `sim` runs the matching
        # simulator target. Keeping both in one stage means the dashboard's
        # language choice does not have to reshuffle the stage list.
        self.python_target = python_target
        self.python_artifact = python_artifact

    def resolve(self, overrides=None):
        """Target and artefact template for the requested testbench language."""
        python = (overrides or {}).get("TB_LANG") == "python"
        target = self.python_target if (python and self.python_target) else self.target
        artifact = (self.python_artifact if (python and self.python_artifact)
                    else self.artifact)
        return target, artifact

    def summary_path(self, design):
        return (os.path.join(PROJECT_DIR, self.summary.format(design=design))
                if self.summary else None)

    def artifact_path(self, design):
        return (os.path.join(PROJECT_DIR, self.artifact.format(design=design))
                if self.artifact else None)

    def image_url(self, design):
        return self.image.format(design=design) if self.image else None


# Timeouts are generous enough for a real block but bounded, so a wedged tool
# eventually releases the worker instead of pinning it forever. Place and
# route is the long pole by a wide margin.
STAGES = {
    "tb": Stage(
        "tb", "Generate Testbench", "tb", 120,
        hint="Builds a harness around the design's real port list.",
        artifact="tb/test_{design}.sv",
        python_artifact="tb/test_{design}.py"),
    "sim": Stage(
        "sim", "Simulate", "sim", 900,
        hint="Runs the testbench under Icarus Verilog and captures a VCD.",
        artifact="results/design.vcd", needs=("tb",),
        python_target="sim-cocotb"),
    "wave": Stage(
        "wave", "Waveform PNG", "wave", 300,
        hint="Renders the VCD to a PNG via gtkwave.",
        artifact="results/design.png", image="/results/design.png"),
    "synth": Stage(
        "synth", "Synthesize", "synth", 900,
        hint="Verilator lint gate, then generic synthesis with Yosys.",
        artifact="results/netlist.v"),
    "schematic": Stage(
        "schematic", "RTL Schematic", "schematic", 600,
        hint="Yosys `show` through Graphviz.",
        artifact="results/{design}.png", image="/results/{design}.png"),
    "pnr": Stage(
        "pnr", "Place & Route", "pnr", 3600,
        hint="Floorplan, PDN, placement, CTS and routing in OpenROAD.",
        summary="results/pnr/{design}_summary.txt",
        artifact="results/pnr/{design}_routed.def",
        image="/results/pnr/{design}_layout.png"),
    "sta": Stage(
        "sta", "Static Timing Analysis", "sta", 900,
        hint="Post-route signoff timing on extracted parasitics.",
        summary="results/pnr/{design}_sta_summary.txt",
        artifact="results/pnr/{design}_sta.rpt", needs=("pnr",)),
    "signoff": Stage(
        "signoff", "Physical Signoff", "signoff", 900,
        hint="Placement legality, unrouted nets, DRC, antennas, PG.",
        summary="results/pnr/{design}_signoff_summary.txt",
        artifact="results/pnr/{design}_signoff.rpt", needs=("pnr",)),
    "report": Stage(
        "report", "Generate Report", "report", 300,
        hint="Collects every artefact and stage summary into one document.",
        summary="results/report/{design}_report.txt",
        artifact="results/report/{design}_report.html"),
    "optimize": Stage(
        "optimize", "Optimize Design", "optimize", 1200,
        hint="Measures structure, area and timing, then ranks what to change.",
        summary="results/optimize/{design}_optimize.txt",
        artifact="results/optimize/{design}_optimize.json"),
}

# What "Generate All" runs, in order. Sequential on purpose: report and
# optimize read what the earlier stages measured, so running them first
# produces a report full of "not run".
ALL_STAGES = ("tb", "sim", "wave", "synth", "schematic",
              "pnr", "sta", "signoff", "optimize", "report")

# The frontend subset, for when the physical backend is not wanted.
FRONTEND_STAGES = ("tb", "sim", "wave", "synth", "schematic",
                   "optimize", "report")

# Stages whose failure should not abandon the rest of a "Generate All" run.
# A missing gtkwave is a system packaging problem, not a design problem, and
# the physical backend needs OpenROAD, which may not be installed at all --
# neither is a reason to skip the report.
NON_FATAL_STAGES = frozenset({"wave", "pnr", "sta", "signoff"})


# ---------------------------------------------------------------------------
# Design discovery
# ---------------------------------------------------------------------------

def clean_name(name):
    """Restrict a design name to safe identifier characters.

    The name becomes a filename and is also interpolated into make variables
    and Tcl, so spaces, slashes, quotes and semicolons all have to be out.
    """
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise FlowError(
            "Invalid name: use letters, digits and underscores only, "
            "starting with a letter or underscore."
        )
    return name


def list_designs():
    """Design names in src/, newest first."""
    paths = sorted(glob.glob(os.path.join(SRC_DIR, "*.sv")),
                   key=os.path.getmtime, reverse=True)
    return [os.path.splitext(os.path.basename(path))[0] for path in paths]


def current_design():
    """The design the Makefile would pick: newest .sv in src/.

    Mirrors the Makefile's `ls -t src/*.sv | head -n1`, so a report read back
    here is the one the stage just wrote.
    """
    designs = list_designs()
    return designs[0] if designs else None


def design_path(design):
    return os.path.join(SRC_DIR, f"{design}.sv")


def read_file(path):
    if path and os.path.isfile(path):
        with open(path, errors="replace") as handle:
            return handle.read()
    return None


# ---------------------------------------------------------------------------
# Design and testbench generation
# ---------------------------------------------------------------------------

TEMPLATE_DESIGN = """`timescale 1ns/1ps
//
// Starter design written by the VeriOpt dashboard.
//
// A single registered bit with an asynchronous reset -- enough to exercise
// every stage of the flow end to end. Replace it, or upload your own RTL,
// and the rest of the flow adapts: the testbench and the constraints are
// both generated from whatever port list the design actually declares.
//
module {name} (
    input  wire clk,
    input  wire rst,
    input  wire data_in,
    output reg  data_out
);

    always @(posedge clk or posedge rst) begin
        if (rst) begin
            data_out <= 1'b0;
        end else begin
            data_out <= data_in;
        end
    end

endmodule
"""


def generate_design(name):
    """Write the starter design.

    Deterministic rather than model-generated. The structure is fully fixed --
    only the module name varies -- so asking a local model to retype it added
    failure modes (a dropped `if (rst)` branch that Yosys then rejects with
    "multiple edge sensitive events", dropped boilerplate, stray commentary)
    with nothing to gain. There is no creative content here.
    """
    name = clean_name(name)
    os.makedirs(SRC_DIR, exist_ok=True)
    path = design_path(name)
    with open(path, "w") as handle:
        handle.write(TEMPLATE_DESIGN.format(name=name))
    # Touch it so `ls -t` picks it as current even if an older file was
    # written in the same second.
    os.utime(path, None)
    return os.path.relpath(path, PROJECT_DIR)


def testbench_path(design, lang):
    suffix = ".py" if lang == "python" else ".sv"
    return os.path.join(TB_DIR, f"test_{design}{suffix}")


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

def _safe_basename(filename):
    """Reduce an uploaded name to a bare, safe basename.

    Browsers can send a path, and an archive can contain one. Anything with a
    directory component or a leading dot is rejected outright rather than
    sanitised, so there is no chance of a surprising destination.
    """
    base = os.path.basename((filename or "").replace("\\", "/")).strip()
    if not base or base.startswith("."):
        raise FlowError(f"Rejected upload name: {filename!r}")
    if not re.match(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$", base):
        raise FlowError(
            f"Rejected upload name {base!r}: use letters, digits, underscore, "
            f"dash and dot only."
        )
    return base


def save_upload(filename, data):
    """Store one uploaded file where the flow expects to find it.

    Returns a list of (relative_path, note). A .zip is expanded, so a
    multi-file design can be uploaded in one go.
    """
    base = _safe_basename(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise FlowError(
            f"{base} is {len(data)/1e6:.1f} MB; the limit is "
            f"{MAX_UPLOAD_BYTES/1e6:.0f} MB."
        )

    extension = os.path.splitext(base)[1].lower()
    if extension == ".zip":
        return _save_zip(base, data)

    destination = UPLOAD_DESTINATIONS.get(extension)
    if destination is None:
        raise FlowError(
            f"{base}: unsupported type {extension or '(none)'}. Accepted: "
            f"{', '.join(sorted(UPLOAD_DESTINATIONS))}, .zip."
        )

    os.makedirs(destination, exist_ok=True)
    path = os.path.join(destination, base)
    with open(path, "wb") as handle:
        handle.write(data)
    os.utime(path, None)
    return [(os.path.relpath(path, PROJECT_DIR), f"{len(data)} bytes")]


def _save_zip(base, data):
    """Expand an uploaded archive, keeping only files the flow can use.

    Members are placed by extension, flattened. Directory structure is
    deliberately discarded: the flow addresses sources as src/<name>.sv, and
    honouring arbitrary archive paths is how a zip escapes its destination.
    """
    import io

    written, skipped = [], []
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise FlowError(f"{base}: not a readable zip ({error})") from error

    with archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = os.path.basename(member.filename.replace("\\", "/"))
            extension = os.path.splitext(name)[1].lower()
            destination = UPLOAD_DESTINATIONS.get(extension)
            if destination is None or not name or name.startswith("."):
                skipped.append(member.filename)
                continue
            if member.file_size > MAX_UPLOAD_BYTES:
                skipped.append(f"{member.filename} (too large)")
                continue
            try:
                safe = _safe_basename(name)
            except FlowError:
                skipped.append(member.filename)
                continue
            os.makedirs(destination, exist_ok=True)
            path = os.path.join(destination, safe)
            with open(path, "wb") as handle:
                handle.write(archive.read(member))
            os.utime(path, None)
            written.append((os.path.relpath(path, PROJECT_DIR),
                            f"{member.file_size} bytes"))

    if not written:
        raise FlowError(
            f"{base}: no usable files. Expected some of "
            f"{', '.join(sorted(UPLOAD_DESTINATIONS))}."
        )
    if skipped:
        written.append((f"{len(skipped)} member(s) skipped",
                        ", ".join(skipped[:6])))
    return written


def inspect_design(design=None):
    """Parse the current design and report its interface.

    This is what tells the user whether an upload was understood, before they
    spend an hour of place and route on it.
    """
    import sys
    sys.path.insert(0, os.path.join(PROJECT_DIR, "scripts"))
    from rtl_parse import parse_sources  # noqa: PLC0415 - optional dependency path

    design = design or current_design()
    if design is None:
        return None

    sources = sorted(
        path for path in glob.glob(os.path.join(SRC_DIR, "*"))
        if os.path.splitext(path)[1].lower() in RTL_EXTENSIONS
    )
    if not sources:
        return None

    parsed = parse_sources(sources, top=design)
    module = parsed["top_module"]
    if module is None:
        return None
    return {
        "design": design,
        "top": module["name"],
        "modules": [m["name"] for m in parsed["modules"]],
        "params": module["params"],
        "ports": module["ports"],
        "sources": [os.path.relpath(path, PROJECT_DIR) for path in sources],
        "top_matches_filename": module["name"] == design,
    }


# ---------------------------------------------------------------------------
# Running a stage
# ---------------------------------------------------------------------------

def make_command(target, overrides=None):
    command = ["make", target]
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            command.append(f"{key}={value}")
    return command


def run_stage(key, overrides=None, on_output=None):
    """Run one stage synchronously. Returns a result dict.

    The stage's summary is read back even when make exits non-zero: a stage
    can fail late -- the layout render is the usual one -- after the reports
    it produced are already valid and worth showing.
    """
    stage = STAGES.get(key)
    if stage is None:
        raise FlowError(f"Unknown stage: {key}")

    design = current_design()
    if design is None:
        raise FlowError("No RTL design in src/. Generate or upload one first.")

    target, artifact_template = stage.resolve(overrides)
    command = make_command(target, overrides)
    started = time.monotonic()
    output_lines = []

    try:
        process = subprocess.Popen(
            command, cwd=PROJECT_DIR, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            # stdin must be closed, not inherited. These are batch tools, but
            # some of them read stdin when they run out of input: Graphviz
            # `dot`, handed a .dot file containing more than one digraph,
            # finishes the file and then blocks reading stdin for further
            # graphs. Inheriting the server's stdin turns that into a hang
            # that outlives the stage and only ends at the timeout.
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        raise FlowError(f"Could not run `{' '.join(command)}`: {error}") from error

    try:
        for line in process.stdout:
            line = line.rstrip("\n")
            output_lines.append(line)
            if on_output:
                on_output(line)
        process.wait(timeout=stage.timeout)
        status = "ok" if process.returncode == 0 else "error"
        note = ("completed" if status == "ok"
                else f"make exited {process.returncode}")
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        status, note = "error", f"timed out after {stage.timeout}s"
        output_lines.append(f"!! {note}")
        if on_output:
            on_output(f"!! {note}")

    elapsed = time.monotonic() - started
    summary_path = stage.summary_path(design)
    artifact_path = (os.path.join(PROJECT_DIR, artifact_template.format(design=design))
                     if artifact_template else None)

    return {
        "stage": key,
        "label": stage.label,
        "design": design,
        "status": status,
        "note": note,
        "elapsed": elapsed,
        "command": " ".join(command),
        "output": "\n".join(output_lines),
        "error": None if status == "ok" else last_error_lines(output_lines),
        "summary": read_file(summary_path),
        "summary_path": (os.path.relpath(summary_path, PROJECT_DIR)
                         if summary_path else None),
        "artifact": (os.path.relpath(artifact_path, PROJECT_DIR)
                     if artifact_path and os.path.exists(artifact_path) else None),
        "image": stage.image_url(design),
    }


def last_error_lines(lines, count=6):
    """The last few meaningful lines of a failed run.

    make always appends its own banner -- "make: *** [Makefile:73: synth]
    Error 1" -- as the final line, and that line alone never says anything.
    The actual cause (a missing tool, a verilator lint failure, a Yosys
    error) is on the lines before it, so a short tail is kept instead.
    """
    meaningful = [line for line in lines if line.strip()]
    return " | ".join(meaningful[-count:]) if meaningful else "no output"


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

_JOB_LOCK = threading.Lock()
_JOBS = {}
_JOB_IDS = itertools.count(1)
_JOB_HISTORY = 40


class Job:
    """A sequence of stages running on a worker thread."""

    def __init__(self, job_id, title, stage_keys, overrides):
        self.id = job_id
        self.title = title
        self.stage_keys = list(stage_keys)
        self.overrides = dict(overrides or {})
        self.status = "queued"
        self.log = []
        self.results = []
        self.current = None
        self.started_at = datetime.now(timezone.utc)
        self.finished_at = None
        self._lock = threading.Lock()

    # -- log ------------------------------------------------------------
    def append(self, line):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self.log.append(f"{stamp}  {line}")
            # A runaway tool can emit a great deal; keep the tail bounded so
            # the dashboard stays responsive.
            if len(self.log) > 4000:
                del self.log[:1000]

    def snapshot(self):
        with self._lock:
            return {
                "id": self.id,
                "title": self.title,
                "status": self.status,
                "current": self.current,
                "stages": list(self.stage_keys),
                "log": list(self.log),
                "results": list(self.results),
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "finished_at": (self.finished_at.isoformat(timespec="seconds")
                                if self.finished_at else None),
            }

    # -- execution -------------------------------------------------------
    def run(self):
        self.status = "running"
        self.append(f"=== {self.title} ===")
        if self.overrides:
            settings = ", ".join(f"{k}={v}" for k, v in self.overrides.items())
            self.append(f"settings: {settings}")

        failed_fatally = False
        for key in self.stage_keys:
            stage = STAGES[key]
            self.current = key
            self.append(f"--- {stage.label} ---")
            try:
                result = run_stage(key, self.overrides, on_output=self.append)
            except FlowError as error:
                result = {
                    "stage": key, "label": stage.label, "status": "error",
                    "note": str(error), "error": str(error), "elapsed": 0.0,
                    "summary": None, "summary_path": None, "artifact": None,
                    "image": None, "output": "", "design": None,
                    "command": f"make {stage.target}",
                }
                self.append(f"!! {error}")

            with self._lock:
                self.results.append(result)

            if result["status"] == "ok":
                self.append(f"{stage.label}: ok ({result['elapsed']:.1f}s)")
            elif key in NON_FATAL_STAGES:
                self.append(f"{stage.label}: FAILED -- {result['note']}; "
                            f"continuing (this stage is not required by the rest)")
            else:
                self.append(f"{stage.label}: FAILED -- {result['note']}")
                failed_fatally = True
                remaining = self.stage_keys[self.stage_keys.index(key) + 1:]
                if remaining:
                    self.append("stopping: later stages depend on this one "
                                f"({', '.join(remaining)} not run)")
                break

        self.current = None
        self.finished_at = datetime.now(timezone.utc)
        errors = [r for r in self.results if r["status"] != "ok"]
        succeeded = [r for r in self.results if r["status"] == "ok"]
        if failed_fatally or not succeeded:
            # "not succeeded" covers the single-stage case. A lone `wave` that
            # failed is in NON_FATAL_STAGES, so nothing was abandoned -- but
            # calling that run "partial" understates it, because the one thing
            # asked for did not happen.
            self.status = "error"
        elif errors:
            self.status = "partial"
        else:
            self.status = "ok"
        self.append(f"=== {self.title}: {self.status} ===")


def start_job(stage_keys, title=None, overrides=None):
    """Start a job on a worker thread and return its id."""
    stage_keys = [key for key in stage_keys if key in STAGES]
    if not stage_keys:
        raise FlowError("No runnable stages requested.")

    if title is None:
        title = (STAGES[stage_keys[0]].label if len(stage_keys) == 1
                 else f"{len(stage_keys)} stages")

    job = Job(next(_JOB_IDS), title, stage_keys, overrides)
    with _JOB_LOCK:
        _JOBS[job.id] = job
        # Keep only recent jobs, so a long-lived server does not grow without
        # bound. A running job is never evicted.
        if len(_JOBS) > _JOB_HISTORY:
            for old_id in sorted(_JOBS)[:-_JOB_HISTORY]:
                if _JOBS[old_id].status not in ("running", "queued"):
                    del _JOBS[old_id]

    thread = threading.Thread(target=job.run, name=f"flow-job-{job.id}",
                              daemon=True)
    thread.start()
    return job.id


def get_job(job_id):
    with _JOB_LOCK:
        job = _JOBS.get(job_id)
    return job.snapshot() if job else None


def latest_job():
    with _JOB_LOCK:
        if not _JOBS:
            return None
        job = _JOBS[max(_JOBS)]
    return job.snapshot()


def job_is_active(job_id):
    snapshot = get_job(job_id)
    return bool(snapshot and snapshot["status"] in ("queued", "running"))


def any_job_active():
    with _JOB_LOCK:
        jobs = list(_JOBS.values())
    return any(job.status in ("queued", "running") for job in jobs)


# ---------------------------------------------------------------------------
# Artefact status, for the dashboard's overview
# ---------------------------------------------------------------------------

def stage_status(design=None, overrides=None):
    """For each stage: whether its artefact exists, and when it was written."""
    design = design or current_design()
    rows = []
    for key in ALL_STAGES:
        stage = STAGES[key]
        _, artifact_template = stage.resolve(overrides)
        path = (os.path.join(PROJECT_DIR, artifact_template.format(design=design))
                if (design and artifact_template) else None)
        summary = stage.summary_path(design) if design else None
        exists = bool(path and os.path.isfile(path))
        rows.append({
            "key": key,
            "label": stage.label,
            "hint": stage.hint,
            "artifact": (os.path.relpath(path, PROJECT_DIR) if path else None),
            "exists": exists,
            "modified": (datetime.fromtimestamp(os.path.getmtime(path))
                         .strftime("%Y-%m-%d %H:%M:%S") if exists else None),
            "size": os.path.getsize(path) if exists else None,
            "summary_path": (os.path.relpath(summary, PROJECT_DIR)
                             if summary else None),
            "has_summary": bool(summary and os.path.isfile(summary)),
        })
    return rows


def summary_text(key, design=None):
    """The summary a stage left behind, or None."""
    design = design or current_design()
    if design is None:
        return None
    stage = STAGES.get(key)
    return read_file(stage.summary_path(design)) if stage else None


def report_paths(design=None):
    design = design or current_design()
    if design is None:
        return None, None
    return (os.path.join(REPORT_DIR, f"{design}_report.txt"),
            os.path.join(REPORT_DIR, f"{design}_report.html"))


# ---------------------------------------------------------------------------
# gtkwave
# ---------------------------------------------------------------------------

def clean_gtkwave_env():
    env = os.environ.copy()
    for var in GTKWAVE_CLEAN_ENV_VARS:
        env.pop(var, None)
    return env


def launch_gtkwave():
    """Open the most recent VCD in gtkwave on the machine running the server.

    This is a desktop window on the host, not something the browser renders --
    the same as running `gtkwave results/design.vcd` by hand, just triggered
    from the dashboard. Local development use only.
    """
    vcd_path = os.path.join(RESULTS_DIR, "design.vcd")
    if not os.path.exists(vcd_path):
        raise FlowError("No VCD at results/design.vcd. Run Simulate first.")
    if not shutil.which("gtkwave"):
        raise FlowError("gtkwave not found. Install with: "
                        "sudo apt-get install gtkwave")
    # Popen, not run: gtkwave is a GUI app that stays open, so blocking the
    # request on it would hang the dashboard for as long as the window lives.
    subprocess.Popen(
        ["gtkwave", vcd_path], cwd=PROJECT_DIR, env=clean_gtkwave_env(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return f"Launched gtkwave for {os.path.relpath(vcd_path, PROJECT_DIR)}"


def tool_availability():
    """Which external tools are present, for the dashboard's status strip."""
    tools = {
        "iverilog": "simulation",
        "vvp": "simulation",
        "verilator": "lint gate before synthesis",
        "yosys": "synthesis and mapping",
        "dot": "RTL schematic",
        "gtkwave": "waveform PNG",
        "openroad": "place and route, signoff",
        "sta": "post-route timing",
        "cocotb-config": "cocotb testbenches",
    }
    return [{"tool": tool, "used_for": purpose,
             "available": shutil.which(tool) is not None}
            for tool, purpose in tools.items()]
