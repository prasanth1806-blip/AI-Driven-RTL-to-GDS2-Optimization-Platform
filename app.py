"""Flask server for the VeriOpt flow: GraphQL API, file serving, and the
Dash dashboard mounted alongside them.

What changed here, and why:

The front end is no longer a hand-written template. templates/index.html was
inline CSS plus four near-duplicate fetch() helpers, and the browser held an
open request for the whole of place and route -- minutes -- so a working run
looked exactly like a hung one. The dashboard is now built in Python
(dashboard.py) and stages run on a worker thread (flow.py), which is what
makes a live log and a progress bar possible.

The resolvers no longer contain the flow. They used to each shell out to make
and re-implement their own error handling, artefact paths and timeouts, which
is how the GraphQL path and the command line drifted apart. Every stage now
goes through flow.run_stage, so `make pnr` in a shell, the dashboard's button
and the placeAndRoute mutation are the same code path.

The schema keeps every field it had, so an existing client still works, and
adds the ones the new buttons need: generateAll, generateReport,
optimizeDesign, uploadDesign, plus job queries for polling a long run.
"""
import base64
import os

from flask import Flask, jsonify, redirect, request, send_from_directory
from graphql import build_schema, graphql_sync

import flow

# No template folder: the dashboard is a Dash app, which serves its own page.
# The Jinja template this used to render is gone.
app = Flask(__name__)

# ---------------------------------------------------------------------------
# Schema
#
# Design is kept as the result type for the original mutations so existing
# queries keep working. Job is new: a long stage returns a job id to poll
# rather than holding the request open.
# ---------------------------------------------------------------------------

schema = build_schema("""
    type Design {
        id: ID!
        name: String!
        filePath: String!
        status: String!
        report: String
    }

    type Port {
        name: String!
        direction: String!
        width: Int
        range: String
        role: String
    }

    type Interface {
        design: String!
        top: String!
        modules: [String!]!
        ports: [Port!]!
        sources: [String!]!
        topMatchesFilename: Boolean!
    }

    type StageResult {
        stage: String!
        label: String!
        status: String!
        note: String
        elapsed: Float
        artifact: String
        summary: String
        error: String
    }

    type Job {
        id: Int!
        title: String!
        status: String!
        current: String
        stages: [String!]!
        log: [String!]!
        results: [StageResult!]!
        startedAt: String
        finishedAt: String
    }

    type StageStatus {
        key: String!
        label: String!
        hint: String
        artifact: String
        exists: Boolean!
        modified: String
        size: Int
    }

    type Tool {
        tool: String!
        usedFor: String!
        available: Boolean!
    }

    type Upload {
        status: String!
        files: [String!]!
        message: String
    }

    type Query {
        hello: String
        designs: [String!]!
        currentDesign: String
        interface: Interface
        stages: [StageStatus!]!
        tools: [Tool!]!
        job(id: Int!): Job
        latestJob: Job
        summary(stage: String!): String
    }

    type Mutation {
        generateDesign(name: String!): Design
        generateTestbench(name: String!, tbType: String!): Design
        simulate: Design
        synthesize: Design
        waveform: Design
        schematic: Design
        placeAndRoute: Design
        staticTimingAnalysis: Design
        physicalSignoff: Design

        generateReport: Design
        optimizeDesign: Design

        uploadDesign(filename: String!, contentBase64: String!): Upload

        runStage(stage: String!, clkPeriod: Float, coreUtil: Int,
                 placeDensity: Float, tbLang: String, tbCycles: Int): Job
        generateAll(includeBackend: Boolean, clkPeriod: Float, coreUtil: Int,
                    placeDensity: Float, tbLang: String, tbCycles: Int): Job
    }
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _overrides(kwargs):
    """Map GraphQL arguments onto make variable overrides."""
    mapping = {
        "clkPeriod": "CLK_PERIOD",
        "coreUtil": "CORE_UTIL",
        "placeDensity": "PLACE_DENSITY",
        "tbLang": "TB_LANG",
        "tbCycles": "TB_CYCLES",
    }
    return {variable: kwargs[key]
            for key, variable in mapping.items()
            if kwargs.get(key) is not None}


def _design_result(result, resolver_id):
    """Render a stage result in the original Design shape.

    The synchronous mutations are kept for compatibility with existing
    clients, which expect `status` to read like "pnr_complete" or
    "synthesis_failed: <reason>".
    """
    stage = result["stage"]
    if result["status"] == "ok":
        status = f"{stage}_complete"
    else:
        status = f"{stage}_failed: {result['error'] or result['note']}"
    return {
        "id": resolver_id,
        "name": result.get("design") or "unknown",
        "filePath": result.get("artifact") or "",
        "status": status,
        "report": result.get("summary"),
    }


def _run_sync(stage, resolver_id, overrides=None):
    try:
        result = flow.run_stage(stage, overrides)
    except flow.FlowError as error:
        return {"id": resolver_id, "name": "unknown", "filePath": "",
                "status": f"{stage}_failed: {error}", "report": None}
    return _design_result(result, resolver_id)


def _job_payload(snapshot):
    if snapshot is None:
        return None
    return {
        "id": snapshot["id"],
        "title": snapshot["title"],
        "status": snapshot["status"],
        "current": snapshot["current"],
        "stages": snapshot["stages"],
        "log": snapshot["log"],
        "results": [
            {
                "stage": result["stage"],
                "label": result["label"],
                "status": result["status"],
                "note": result.get("note"),
                "elapsed": result.get("elapsed"),
                "artifact": result.get("artifact"),
                "summary": result.get("summary"),
                "error": result.get("error"),
            }
            for result in snapshot["results"]
        ],
        "startedAt": snapshot["started_at"],
        "finishedAt": snapshot["finished_at"],
    }


# ---------------------------------------------------------------------------
# Query resolvers
# ---------------------------------------------------------------------------

def resolve_hello(obj, info):
    return "GraphQL backend is alive!"


def resolve_designs(obj, info):
    return flow.list_designs()


def resolve_current_design(obj, info):
    return flow.current_design()


def resolve_interface(obj, info):
    info_dict = flow.inspect_design()
    if info_dict is None:
        return None
    return {
        "design": info_dict["design"],
        "top": info_dict["top"],
        "modules": info_dict["modules"],
        "sources": info_dict["sources"],
        "topMatchesFilename": info_dict["top_matches_filename"],
        "ports": [
            {
                "name": port["name"],
                "direction": port["direction"],
                "width": port["width"],
                "range": (f"[{port['msb']}:{port['lsb']}]"
                          if port["vector"] else ""),
                "role": ("clock" if port["is_clock"] else
                         "reset (active low)"
                         if port["is_reset"] and port["active_low"]
                         else "reset" if port["is_reset"] else ""),
            }
            for port in info_dict["ports"]
        ],
    }


def resolve_stages(obj, info):
    return [
        {
            "key": row["key"], "label": row["label"], "hint": row["hint"],
            "artifact": row["artifact"], "exists": row["exists"],
            "modified": row["modified"], "size": row["size"],
        }
        for row in flow.stage_status()
    ]


def resolve_tools(obj, info):
    return [{"tool": entry["tool"], "usedFor": entry["used_for"],
             "available": entry["available"]}
            for entry in flow.tool_availability()]


def resolve_job(obj, info, id):  # noqa: A002 - GraphQL argument name
    return _job_payload(flow.get_job(id))


def resolve_latest_job(obj, info):
    return _job_payload(flow.latest_job())


def resolve_summary(obj, info, stage):
    return flow.summary_text(stage)


# ---------------------------------------------------------------------------
# Mutation resolvers
# ---------------------------------------------------------------------------

def resolve_generate_design(obj, info, name):
    try:
        path = flow.generate_design(name)
    except flow.FlowError as error:
        return {"id": "1", "name": name, "filePath": "",
                "status": f"error: {error}", "report": None}
    return {"id": "1", "name": name, "filePath": path,
            "status": "rtl_generated", "report": None}


def resolve_generate_testbench(obj, info, name, tbType):
    """Generate a testbench for the named design.

    The harness is built from the design's real port list by
    scripts/gen_tb.py, so this works on an uploaded design. The generator it
    replaced emitted a fixed clk/rst/data_in/data_out harness, which only ever
    matched the built-in template and produced an uncompilable testbench for
    anything else.
    """
    try:
        name = flow.clean_name(name)
    except flow.FlowError as error:
        return {"id": "2", "name": name, "filePath": "",
                "status": f"error: {error}", "report": None}

    if not os.path.isfile(flow.design_path(name)):
        return {"id": "2", "name": name, "filePath": "",
                "status": f"error: no RTL at src/{name}.sv", "report": None}

    lang = "python" if tbType.lower() in ("python", "py", "cocotb") else "sv"
    result = _run_sync("tb", "2", {"TB_LANG": lang})
    if result["status"] == "tb_complete":
        result["status"] = "tb_generated"
        result["filePath"] = os.path.relpath(
            flow.testbench_path(name, lang), flow.PROJECT_DIR)
    result["name"] = f"{name}_tb"
    return result


def resolve_simulate(obj, info):
    return _run_sync("sim", "3")


def resolve_synthesize(obj, info):
    return _run_sync("synth", "4")


def resolve_waveform(obj, info):
    return _run_sync("wave", "5")


def resolve_schematic(obj, info):
    return _run_sync("schematic", "6")


def resolve_place_and_route(obj, info):
    """Map to the PDK's standard cells and run OpenROAD place and route.

    Covers the whole physical flow: technology mapping, floorplan, pin
    placement, tapcells, power grid, placement, clock tree synthesis, global
    and detailed routing, antenna repair, fillers, parasitic extraction and a
    rendered layout PNG. Minutes, not seconds -- prefer the generateAll or
    runStage mutations, which return a job to poll instead of holding the
    request open for the duration.
    """
    return _run_sync("pnr", "7")


def resolve_sta(obj, info):
    """Post-route signoff timing under standalone OpenSTA.

    Reads the routed netlist plus the extracted SPEF, so delays come from
    measured parasitics rather than the estimates place and route optimises
    against. Requires a completed placeAndRoute.
    """
    return _run_sync("sta", "8")


def resolve_signoff(obj, info):
    """Physical signoff checks on the routed database.

    Placement legality, unrouted nets, routing DRC, antenna violations, power
    grid connectivity and the library's electrical limits. Requires a
    completed placeAndRoute.
    """
    return _run_sync("signoff", "9")


def resolve_generate_report(obj, info):
    return _run_sync("report", "10")


def resolve_optimize(obj, info):
    return _run_sync("optimize", "11")


def resolve_upload_design(obj, info, filename, contentBase64):
    try:
        data = base64.b64decode(contentBase64, validate=True)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return {"status": "error", "files": [],
                "message": f"contentBase64 is not valid base64: {error}"}
    try:
        written = flow.save_upload(filename, data)
    except flow.FlowError as error:
        return {"status": "error", "files": [], "message": str(error)}
    return {"status": "ok", "files": [path for path, _ in written],
            "message": f"{len(written)} entr(y/ies) written"}


def resolve_run_stage(obj, info, stage, **kwargs):
    try:
        job_id = flow.start_job([stage], overrides=_overrides(kwargs))
    except flow.FlowError as error:
        raise ValueError(str(error)) from error
    return _job_payload(flow.get_job(job_id))


def resolve_generate_all(obj, info, includeBackend=True, **kwargs):
    stages = flow.ALL_STAGES if includeBackend else flow.FRONTEND_STAGES
    title = "Generate All" if includeBackend else "Generate All (front end)"
    job_id = flow.start_job(stages, title=title, overrides=_overrides(kwargs))
    return _job_payload(flow.get_job(job_id))


query = schema.get_type("Query").fields
query["hello"].resolve = resolve_hello
query["designs"].resolve = resolve_designs
query["currentDesign"].resolve = resolve_current_design
query["interface"].resolve = resolve_interface
query["stages"].resolve = resolve_stages
query["tools"].resolve = resolve_tools
query["job"].resolve = resolve_job
query["latestJob"].resolve = resolve_latest_job
query["summary"].resolve = resolve_summary

mutation = schema.get_type("Mutation").fields
mutation["generateDesign"].resolve = resolve_generate_design
mutation["generateTestbench"].resolve = resolve_generate_testbench
mutation["simulate"].resolve = resolve_simulate
mutation["synthesize"].resolve = resolve_synthesize
mutation["waveform"].resolve = resolve_waveform
mutation["schematic"].resolve = resolve_schematic
mutation["placeAndRoute"].resolve = resolve_place_and_route
mutation["staticTimingAnalysis"].resolve = resolve_sta
mutation["physicalSignoff"].resolve = resolve_signoff
mutation["generateReport"].resolve = resolve_generate_report
mutation["optimizeDesign"].resolve = resolve_optimize
mutation["uploadDesign"].resolve = resolve_upload_design
mutation["runStage"].resolve = resolve_run_stage
mutation["generateAll"].resolve = resolve_generate_all


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return redirect("/dashboard/")


@app.route("/graphql", methods=["POST"])
def graphql_server():
    payload = request.get_json(silent=True) or {}
    result = graphql_sync(schema, payload.get("query"),
                          variable_values=payload.get("variables"))
    response = {}
    if result.data is not None:
        response["data"] = result.data
    if result.errors:
        response["errors"] = [error.formatted for error in result.errors]
    return jsonify(response)


@app.route("/api/view-waveform", methods=["POST"])
def view_waveform():
    """Open the most recent VCD in gtkwave on the machine running the server.

    A desktop window on the host, not something the browser renders -- the
    same as running `gtkwave results/design.vcd` by hand. Local development
    use only.
    """
    try:
        message = flow.launch_gtkwave()
    except flow.FlowError as error:
        return jsonify({"status": "error", "message": str(error)}), 404
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return jsonify({"status": "error",
                        "message": f"Failed to launch gtkwave: {error}"}), 500
    return jsonify({"status": "ok", "message": message}), 200


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accept RTL, testbench, constraint or zip uploads as multipart form data.

    The dashboard uploads through Dash's own component; this endpoint exists so
    the same thing can be done with curl or from a script.
    """
    files = request.files.getlist("file")
    if not files:
        return jsonify({"status": "error",
                        "message": "No file part named 'file'."}), 400

    written, failed = [], []
    for handle in files:
        try:
            written.extend(flow.save_upload(handle.filename, handle.read()))
        except flow.FlowError as error:
            failed.append(str(error))

    status = 200 if written and not failed else (207 if written else 400)
    return jsonify({
        "status": "ok" if written and not failed else
                  "partial" if written else "error",
        "files": [path for path, _ in written],
        "errors": failed,
    }), status


@app.route("/results/<path:filename>")
def serve_result_file(filename):
    """Serve files from results/ so the dashboard can show generated PNGs and
    the HTML report.

    send_from_directory rejects '..' and absolute paths on its own, so this
    cannot reach outside results/.
    """
    return send_from_directory(os.path.abspath(flow.RESULTS_DIR), filename)


# The dashboard is created at import time so `flask run` and `python app.py`
# both serve it, and so a Dash callback registration error surfaces at startup
# rather than on the first click.
from dashboard import create_dashboard  # noqa: E402 - needs `app` to exist

dash_app = create_dashboard(app, url_base="/dashboard/")


if __name__ == "__main__":
    # debug=False: the reloader starts a second process, and a flow job runs on
    # a worker thread inside one of them. A reload mid-run would leave the
    # dashboard polling a job id that no longer exists in the surviving process.
    app.run(host="127.0.0.1", port=5000, debug=False)
