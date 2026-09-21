"""Dash dashboard for VeriOpt.

This dashboard is deliberately explicit about the real EDA flow:

    RTL -> Verification -> Waveform -> Synthesis -> P&R -> STA -> Signoff
                                           -> Report -> Optimization

The UI does not claim that AI verified or synthesized a design.  The external
EDA tools remain the source of truth.  The optimization stage currently shows
the deterministic analysis produced by scripts/optimize.py; the LLM/AI layer
can be added on top of these structured results later.

The dashboard is mounted onto the existing Flask app by app.py.  No new
backend API is required: it uses the existing flow.py functions and job
runner.
"""

import base64
import json
import os
import time

import dash
import dash_bootstrap_components as dbc
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

import flow

POLL_MS = 1000

STATUS_COLOURS = {
    "ok": "success",
    "error": "danger",
    "partial": "warning",
    "running": "primary",
    "queued": "secondary",
}

# Logical UI grouping. These keys must match flow.STAGES.
FRONTEND_BUTTONS = ("tb", "sim", "wave", "synth", "schematic")
BACKEND_BUTTONS = ("pnr", "sta", "signoff")
ANALYSIS_BUTTONS = ("report", "optimize")

PIPELINE = (
    ("tb", "1", "Verification", "Prepare Cocotb testbench"),
    ("sim", "2", "Verification", "Run Cocotb + Icarus"),
    ("wave", "3", "Verification", "Create waveform view"),
    ("synth", "4", "Logic Synthesis", "RTL -> gate netlist with Yosys"),
    ("schematic", "5", "Logic Synthesis", "Render synthesized logic"),
    ("pnr", "6", "Physical Design", "Floorplan -> placement -> CTS -> routing"),
    ("sta", "7", "Signoff", "Post-route static timing analysis"),
    ("signoff", "8", "Signoff", "Physical checks"),
    ("report", "9", "Analysis", "Collect all available results"),
    ("optimize", "10", "Analysis", "Rank deterministic optimization opportunities"),
)

STAGE_EXPLANATIONS = {
    "tb": {
        "input": "RTL source in src/",
        "tool": "gen_tb.py",
        "output": "tb/test_<design>.py",
        "proves": "A runnable verification harness exists; it does not prove functional correctness by itself.",
    },
    "sim": {
        "input": "RTL + Cocotb testbench",
        "tool": "Cocotb + Icarus Verilog",
        "output": "results/verification.json + results/design.vcd",
        "proves": "The supplied/generated tests executed successfully. Functional correctness is only claimed when assertion-based functional tests pass.",
    },
    "wave": {
        "input": "results/design.vcd",
        "tool": "GTKWave/rendering helper",
        "output": "results/design.png",
        "proves": "A visual representation of simulated signals; it is not a correctness proof.",
    },
    "synth": {
        "input": "RTL source",
        "tool": "Verilator lint + Yosys",
        "output": "results/netlist.v and synthesis reports",
        "proves": "The RTL passed the synthesis/lint flow and was converted into logic.",
    },
    "schematic": {
        "input": "RTL/netlist",
        "tool": "Yosys + Graphviz",
        "output": "results/<design>.png",
        "proves": "A visual logic representation was generated.",
    },
    "pnr": {
        "input": "Mapped netlist + constraints + PDK",
        "tool": "OpenROAD",
        "output": "routed DEF, layout image and P&R summaries",
        "proves": "A physical implementation was attempted through floorplan, placement, CTS and routing.",
    },
    "sta": {
        "input": "Routed design + SDC/SPEF",
        "tool": "OpenSTA",
        "output": "STA report + timing summary",
        "proves": "Timing was analyzed against the supplied constraints.",
    },
    "signoff": {
        "input": "Physical database/layout",
        "tool": "OpenROAD/checkers",
        "output": "Physical signoff report",
        "proves": "The configured physical checks were executed; inspect individual checks for tapeout readiness.",
    },
    "report": {
        "input": "All available stage artifacts",
        "tool": "gen_report.py",
        "output": "results/report/<design>_report.*",
        "proves": "Results were collected into one report; it does not create missing measurements.",
    },
    "optimize": {
        "input": "RTL structure + available EDA metrics",
        "tool": "scripts/optimize.py",
        "output": "results/optimize/<design>_optimize.json/txt",
        "proves": "Nothing about correctness. It ranks engineering opportunities from measured evidence.",
    },
}


def create_dashboard(server, url_base="/dashboard/"):
    """Attach the dashboard to the existing Flask application."""
    app = dash.Dash(
        __name__,
        server=server,
        url_base_pathname=url_base,
        external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.BOOTSTRAP],
        title="VeriOpt — RTL to GDSII",
        suppress_callback_exceptions=True,
    )
    app.layout = _layout()
    _register_callbacks(app)
    return app


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def _layout():
    return dbc.Container(
        [
            dcc.Store(id="job-id"),
            dcc.Store(id="settings"),
            dcc.Interval(id="poll", interval=POLL_MS, disabled=True),
            _navbar(),
            _pipeline_legend(),
            dbc.Row(
                [
                    dbc.Col(_control_column(), lg=4, className="mb-3"),
                    dbc.Col(_output_column(), lg=8),
                ],
                className="g-3",
            ),
            _footer(),
        ],
        fluid=True,
        className="py-3",
    )


def _navbar():
    return dbc.Navbar(
        dbc.Container(
            [
                dbc.NavbarBrand(
                    [html.I(className="bi bi-cpu me-2"), "VeriOpt"],
                    className="fw-semibold fs-4",
                ),
                html.Div(
                    [
                        html.Span("RTL → GDSII", className="badge text-bg-light me-2"),
                        html.Span(id="design-badge"),
                    ],
                    className="ms-auto",
                ),
            ],
            fluid=True,
        ),
        color="dark",
        dark=True,
        className="mb-3 rounded",
    )


def _pipeline_legend():
    return dbc.Alert(
        [
            html.Strong("How VeriOpt works: "),
            html.Span(
                "the EDA tools execute the flow; VeriOpt records their artifacts and "
                "metrics; the optimization/AI layer analyzes those results."
            ),
        ],
        color="light",
        className="border py-2 small",
    )


def _control_column():
    return [_design_card(), _run_card(), _settings_card()]


def _design_card():
    return dbc.Card(
        [
            dbc.CardHeader(
                [html.I(className="bi bi-file-earmark-code me-2"), "Design Input"]
            ),
            dbc.CardBody(
                [
                    dbc.Label("Active design", html_for="design-select"),
                    dcc.Dropdown(
                        id="design-select",
                        clearable=False,
                        placeholder="No design yet",
                    ),
                    html.Div(id="design-note", className="form-text mb-3"),

                    dbc.Label("New starter design", html_for="design-name"),
                    dbc.InputGroup(
                        [
                            dbc.Input(
                                id="design-name",
                                placeholder="e.g. counter",
                                debounce=True,
                            ),
                            dbc.Button(
                                "Create",
                                id="btn-create",
                                color="secondary",
                                outline=True,
                            ),
                        ],
                        className="mb-1",
                    ),
                    html.Div(
                        "Creates a small registered RTL block for exercising the flow. "
                        "Upload real RTL for a real design.",
                        className="form-text mb-3",
                    ),

                    dbc.Label("Upload design"),
                    dcc.Upload(
                        id="upload-design",
                        multiple=True,
                        children=html.Div(
                            [
                                html.I(
                                    className="bi bi-cloud-arrow-up fs-4 d-block mb-1 text-secondary"
                                ),
                                html.Span("Drop files or click to browse", className="small"),
                                html.Br(),
                                html.Span(
                                    ".sv .v .svh .vh · .py testbench · .sdc constraints · .zip",
                                    className="form-text",
                                ),
                            ],
                            className="text-center py-3",
                        ),
                        className="border border-2 border-dashed rounded mb-2",
                        style={"cursor": "pointer"},
                    ),
                    html.Div(
                        "The uploaded RTL becomes the flow input. A supplied Cocotb testbench "
                        "is preserved; otherwise VeriOpt can generate a smoke harness.",
                        className="form-text",
                    ),
                    html.Div(id="upload-result", className="mt-2"),
                ]
            ),
        ],
        className="mb-3 shadow-sm",
    )


def _run_card():
    return dbc.Card(
        [
            dbc.CardHeader(
                [html.I(className="bi bi-play-circle me-2"), "Run the EDA Flow"]
            ),
            dbc.CardBody(
                [
                    dbc.Button(
                        [
                            html.I(className="bi bi-lightning-charge-fill me-2"),
                            "Run RTL → GDSII Flow",
                        ],
                        id="btn-all",
                        color="primary",
                        className="w-100 mb-2",
                    ),
                    dbc.Checklist(
                        id="all-scope",
                        options=[
                            {
                                "label": " Include physical backend (P&R, STA, signoff)",
                                "value": "backend",
                            }
                        ],
                        value=["backend"],
                        switch=True,
                        className="mb-3 small",
                    ),
                    html.Div(
                        "Executes stages sequentially. A failed mandatory stage stops dependent stages.",
                        className="form-text mb-3",
                    ),

                    _section_label("01 · VERIFICATION"),
                    _stage_buttons(FRONTEND_BUTTONS[:3], "secondary"),

                    _section_label("02 · LOGIC SYNTHESIS", top=True),
                    _stage_buttons(FRONTEND_BUTTONS[3:], "secondary"),

                    _section_label("03 · PHYSICAL DESIGN", top=True),
                    _stage_buttons(BACKEND_BUTTONS, "dark"),

                    _section_label("04 · ANALYSIS", top=True),
                    _stage_buttons(ANALYSIS_BUTTONS, "info"),

                    html.Hr(),
                    dbc.Button(
                        [html.I(className="bi bi-activity me-2"), "Open VCD in GTKWave"],
                        id="btn-gtkwave",
                        color="light",
                        size="sm",
                        className="w-100",
                    ),
                    html.Div(
                        "GTKWave opens on the machine running the VeriOpt server.",
                        className="form-text mt-1",
                    ),
                    html.Div(id="gtkwave-result", className="mt-2"),
                ]
            ),
        ],
        className="mb-3 shadow-sm",
    )


def _section_label(text, top=False):
    return html.Div(
        text,
        className=("text-uppercase fw-semibold small text-secondary "
                   + ("mt-3 " if top else "")
                   + "mb-1"),
    )


def _stage_buttons(keys, colour):
    return html.Div(
        [
            dbc.Button(
                flow.STAGES[key].label,
                id={"role": "stage", "stage": key},
                color=colour,
                outline=True,
                size="sm",
                className="me-1 mb-1",
                title=flow.STAGES[key].hint,
            )
            for key in keys
        ]
    )


def _settings_card():
    return dbc.Card(
        [
            dbc.CardHeader(
                [html.I(className="bi bi-sliders me-2"), "Flow Settings"]
            ),
            dbc.CardBody(
                [
                    dbc.Label("Verification framework", className="small"),
                    dbc.RadioItems(
                        id="tb-lang",
                        options=[
                            {"label": " Python (Cocotb) — recommended", "value": "python"},
                            {"label": " SystemVerilog (Icarus) — compatibility", "value": "sv"},
                        ],
                        value="python",
                        className="mb-3 small",
                    ),
                    dbc.Alert(
                        [
                            html.Strong("Default: "),
                            "Cocotb + Icarus. If tb/test_<design>.py exists, the flow uses it "
                            "instead of replacing it.",
                        ],
                        color="info",
                        className="py-2 px-3 small",
                    ),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label("Clock period (ns)", className="small"),
                                    dbc.Input(
                                        id="clk-period",
                                        type="number",
                                        value=10.0,
                                        min=0.1,
                                        step=0.1,
                                        size="sm",
                                    ),
                                ],
                                width=6,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label("Core utilisation (%)", className="small"),
                                    dbc.Input(
                                        id="core-util",
                                        type="number",
                                        value=40,
                                        min=1,
                                        max=95,
                                        step=1,
                                        size="sm",
                                    ),
                                ],
                                width=6,
                            ),
                        ],
                        className="mb-2 g-2",
                    ),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label("Place density", className="small"),
                                    dbc.Input(
                                        id="place-density",
                                        type="number",
                                        value=0.60,
                                        min=0.05,
                                        max=1.0,
                                        step=0.05,
                                        size="sm",
                                    ),
                                ],
                                width=6,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label("Stimulus cycles", className="small"),
                                    dbc.Input(
                                        id="tb-cycles",
                                        type="number",
                                        value=32,
                                        min=1,
                                        step=1,
                                        size="sm",
                                    ),
                                ],
                                width=6,
                            ),
                        ],
                        className="g-2",
                    ),
                    html.Div(
                        "These values are passed to make, so dashboard and shell runs use the same flow variables.",
                        className="form-text mt-2",
                    ),
                ]
            ),
        ],
        className="mb-3 shadow-sm",
    )


def _output_column():
    return dbc.Card(
        [
            dbc.CardHeader(
                dbc.Tabs(
                    [
                        dbc.Tab(label="Pipeline", tab_id="tab-overview"),
                        dbc.Tab(label="Metrics", tab_id="tab-report"),
                        dbc.Tab(label="Optimization", tab_id="tab-optimize"),
                        dbc.Tab(label="Views", tab_id="tab-views"),
                        dbc.Tab(label="Console", tab_id="tab-console"),
                    ],
                    id="tabs",
                    active_tab="tab-overview",
                )
            ),
            dbc.CardBody(
                [
                    html.Div(id="job-banner", className="mb-3"),
                    html.Div(id="tab-content"),
                ]
            ),
        ],
        className="shadow-sm",
    )


def _footer():
    return html.Div(
        [html.Hr(), html.Div(id="tool-strip", className="small text-secondary")],
        className="mt-3",
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _alert(message, colour="secondary", icon=None):
    body = [html.I(className=f"bi {icon} me-2")] if icon else []
    return dbc.Alert(body + [message], color=colour, className="py-2 px-3 mb-0 small")


def _mono(text, height="58vh"):
    return html.Pre(
        text,
        className="bg-light border rounded p-3 mb-0 small",
        style={
            "maxHeight": height,
            "overflow": "auto",
            "whiteSpace": "pre",
            "fontSize": "0.76rem",
        },
    )


def _safe_read_json(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _status_badge(state):
    labels = {
        "completed": ("PASS", "success"),
        "failed": ("FAIL", "danger"),
        "running": ("RUNNING", "primary"),
        "ready": ("READY", "warning"),
        "waiting": ("WAITING", "light"),
        "not_run": ("NOT RUN", "light"),
    }
    text, colour = labels.get(state, (state.upper(), "secondary"))
    return dbc.Badge(text, color=colour, text_color="secondary" if colour == "light" else None)


def _job_result_map(snapshot):
    if not snapshot:
        return {}
    return {item.get("stage"): item for item in snapshot.get("results", [])}


def _stage_state(key, status_rows, result_map, current=None):
    if current == key:
        return "running"
    result = result_map.get(key)
    if result:
        return "completed" if result.get("status") == "ok" else "failed"
    row = status_rows.get(key, {})
    if row.get("exists"):
        return "completed"

    needs = flow.STAGES[key].needs
    if needs:
        missing = [need for need in needs if status_rows.get(need, {}).get("exists") is not True]
        if missing:
            return "waiting"
    return "ready" if key == "tb" else "not_run"


def _pipeline_view(design, settings, snapshot):
    if design is None:
        return _alert(
            "No RTL design is selected. Create a starter design or upload your RTL to begin.",
            "info",
            "bi-info-circle",
        )

    status_rows = {row["key"]: row for row in flow.stage_status(design, settings)}
    result_map = _job_result_map(snapshot)
    current = snapshot.get("current") if snapshot else None

    blocks = [_design_summary(design), html.Hr(), _flow_pipeline(design, status_rows, result_map, current)]
    blocks.extend([html.Hr(), _verification_summary(design), html.Hr(), _next_action(design, status_rows, result_map, current)])
    return html.Div(blocks)


def _design_summary(design):
    info = flow.inspect_design(design)
    if info is None:
        return _alert(
            f"Could not parse src/{design}.sv. Check the RTL and top-module name.",
            "warning",
            "bi-exclamation-triangle",
        )

    roles = []
    for port in info["ports"]:
        if port["is_clock"]:
            roles.append("clock")
        if port["is_reset"]:
            roles.append("reset")
    features = []
    if roles:
        features.append(" / ".join(sorted(set(roles))))
    if any(m for m in info.get("modules", []) if m != info["top"]):
        features.append(f"{len(info['modules'])} modules")
    if any("memory" in str(m).lower() for m in info.get("modules", [])):
        features.append("memory");

    header = dbc.Row(
        [
            dbc.Col(
                [
                    html.Div("ACTIVE RTL", className="text-secondary small fw-semibold"),
                    html.H4(info["top"], className="mb-1"),
                    html.Div(
                        f"{len(info['sources'])} source file(s) · {len(info['ports'])} top-level ports",
                        className="text-secondary small",
                    ),
                ],
                md=7,
            ),
            dbc.Col(
                dbc.Alert(
                    [
                        html.Strong("Input ready"),
                        html.Br(),
                        html.Span(" → Start with verification", className="small"),
                    ],
                    color="success",
                    className="py-2 px-3 mb-0",
                ),
                md=5,
            ),
        ],
        className="g-2",
    )

    if not info["top_matches_filename"]:
        header = html.Div(
            [
                header,
                _alert(
                    f"Top module is '{info['top']}', but the selected design is '{design}'. "
                    "The physical flow is filename-driven; make sure DESIGN/top naming is consistent.",
                    "warning",
                    "bi-exclamation-triangle",
                ),
            ]
        )

    return html.Div([header, _port_table(info["ports"])])


def _port_table(ports):
    rows = []
    for port in ports:
        role = (
            "clock"
            if port["is_clock"]
            else "reset (active low)"
            if port["is_reset"] and port["active_low"]
            else "reset"
            if port["is_reset"]
            else ""
        )
        rows.append(
            html.Tr(
                [
                    html.Td(html.Code(port["name"])),
                    html.Td(port["direction"]),
                    html.Td(
                        port["width"] if port["width"] is not None else "?",
                        className="text-end",
                    ),
                    html.Td(
                        html.Code(f"[{port['msb']}:{port['lsb']}]") if port["vector"] else ""
                    ),
                    html.Td(html.Span(role, className="text-secondary small")),
                ]
            )
        )
    return dbc.Table(
        [
            html.Thead(html.Tr([html.Th("Port"), html.Th("Direction"), html.Th("Width", className="text-end"), html.Th("Range"), html.Th("Role")])),
            html.Tbody(rows),
        ],
        size="sm",
        hover=True,
        responsive=True,
        className="mt-3 mb-0",
    )


def _flow_pipeline(design, status_rows, result_map, current):
    cards = []
    for index, (key, number, group, description) in enumerate(PIPELINE):
        stage = flow.STAGES[key]
        state = _stage_state(key, status_rows, result_map, current)
        result = result_map.get(key)
        row = status_rows.get(key, {})

        if result:
            detail = result.get("note") or result.get("error") or "stage finished"
            elapsed = result.get("elapsed")
            if elapsed is not None:
                detail = f"{detail} · {elapsed:.1f}s"
        elif row.get("exists"):
            detail = f"Artifact: {row.get('artifact') or 'present'}"
        elif state == "waiting":
            detail = "Waiting for prerequisite stage(s)."
        elif state == "running":
            detail = "EDA command is currently running."
        else:
            detail = stage.hint

        explanation = STAGE_EXPLANATIONS.get(key, {})
        cards.append(
            dbc.Card(
                dbc.CardBody(
                    [
                        dbc.Row(
                            [
                                dbc.Col(
                                    html.Div(number, className="rounded-circle border text-center fw-bold py-1"),
                                    width=1,
                                ),
                                dbc.Col(
                                    [
                                        html.Div(group.upper(), className="text-secondary small fw-semibold"),
                                        html.Div(stage.label, className="fw-semibold"),
                                        html.Div(description, className="small text-secondary"),
                                    ],
                                    width=7,
                                ),
                                dbc.Col(_status_badge(state), width=4, className="text-end"),
                            ],
                            className="align-items-center g-2",
                        ),
                        html.Div(detail, className="small mt-2"),
                        html.Div(
                            [
                                html.Span("Input: ", className="fw-semibold"),
                                html.Code(explanation.get("input", "-")),
                                html.Span("  ·  Tool: ", className="fw-semibold ms-2"),
                                html.Code(explanation.get("tool", "-")),
                                html.Span("  ·  Output: ", className="fw-semibold ms-2"),
                                html.Code(explanation.get("output", "-")),
                            ],
                            className="small text-secondary mt-1",
                        ),
                    ],
                    className="py-2",
                ),
                className="mb-2",
            )
        )

    return html.Div(
        [
            html.Div("EXECUTION PIPELINE", className="fw-semibold mb-2"),
            html.Div(
                "Each card shows what goes in, which tool runs, what artifact comes out, and what the stage actually establishes.",
                className="text-secondary small mb-3",
            ),
            *cards,
        ]
    )


def _verification_summary(design):
    path = os.path.join(flow.RESULTS_DIR, "verification.json")
    data = _safe_read_json(path)
    if not data:
        return html.Div(
            [
                html.Div("VERIFICATION", className="fw-semibold"),
                _alert(
                    "No verification.json yet. Run Functional Verification. Cocotb/Icarus results are the source of truth.",
                    "light",
                    "bi-info-circle",
                ),
            ],
            className="mt-1",
        )

    status = str(data.get("status", "UNKNOWN")).upper()
    level = data.get("verification_level", "unknown")
    proven = bool(data.get("functional_correctness_proven"))
    assertion = bool(data.get("assertion_based_testbench"))
    status_colour = "success" if status == "PASS" else "danger"

    cards = [
        _metric_card("Result", status, status_colour),
        _metric_card("Level", level.replace("_", " ").title(), "info"),
        _metric_card("Assertions", "YES" if assertion else "NO", "success" if assertion else "warning"),
        _metric_card("Correctness proven", "YES" if proven else "NO", "success" if proven else "warning"),
    ]
    return html.Div(
        [
            html.Div("VERIFICATION", className="fw-semibold mb-2"),
            dbc.Row(cards, className="g-2"),
            html.Div(
                "Correctness is marked proven only for a passing assertion-based functional testbench; a generated smoke test is intentionally not treated as a functional proof.",
                className="text-secondary small mt-2",
            ),
        ]
    )


def _metric_card(label, value, colour="secondary"):
    return dbc.Col(
        dbc.Card(
            dbc.CardBody(
                [
                    html.Div(label, className="text-secondary small"),
                    html.Div(value, className="fw-semibold fs-5"),
                ],
                className="py-2",
            ),
            className=f"border-{colour}",
        ),
        md=3,
        xs=6,
    )


def _next_action(design, status_rows, result_map, current):
    if current:
        return dbc.Alert(
            [html.Strong("Currently running: "), flow.STAGES[current].label, ". " , flow.STAGES[current].hint],
            color="primary",
            className="py-2 px-3 small",
        )

    for key, _, _, _ in PIPELINE:
        state = _stage_state(key, status_rows, result_map, None)
        if state in {"ready", "not_run"}:
            if key == "tb":
                return _alert("Next: prepare the Cocotb testbench, then run verification.", "info", "bi-arrow-right")
            return _alert(f"Next: {flow.STAGES[key].label}. {flow.STAGES[key].hint}", "info", "bi-arrow-right")
        if state == "failed":
            return _alert(f"Attention: {flow.STAGES[key].label} failed. Open Console to inspect the tool output.", "danger", "bi-exclamation-triangle")

    return _alert("All available stages have artifacts. Review Metrics and Optimization.", "success", "bi-check-circle")


def _metrics_tab(design):
    if design is None:
        return _alert("No design yet.", "info", "bi-info-circle")

    blocks = [
        html.Div("EDA RESULTS", className="fw-semibold mb-1"),
        html.Div(
            "These values come from generated result files. Missing values are shown as missing rather than guessed.",
            className="text-secondary small mb-3",
        ),
    ]

    verification = _safe_read_json(os.path.join(flow.RESULTS_DIR, "verification.json")) or {}
    optimize = _safe_read_json(os.path.join(flow.RESULTS_DIR, "optimize", f"{design}_optimize.json")) or {}

    values = [
        ("Verification", verification.get("status", "not measured")),
        ("Verification level", verification.get("verification_level", "not measured")),
        ("Functional correctness", "PROVEN" if verification.get("functional_correctness_proven") else "not proven"),
        ("Optimization analysis", "available" if optimize else "not measured"),
    ]

    # Try to surface common metric names without assuming a single schema.
    metrics = _flatten_metrics(optimize)
    for label, keys in [
        ("Area", ("area", "total_area", "mapped_area")),
        ("Utilisation", ("utilisation", "utilization", "core_util")),
        ("Total power", ("total_power", "power")),
        ("WNS", ("wns", "worst_negative_slack")),
        ("TNS", ("tns", "total_negative_slack")),
        ("Routing DRC errors", ("routing_drc_errors", "drc_errors")),
    ]:
        found = _first_value(metrics, keys)
        values.append((label, found if found is not None else "not measured"))

    table = dbc.Table(
        [
            html.Thead(html.Tr([html.Th("Metric"), html.Th("Value"), html.Th("Meaning")])),
            html.Tbody(
                [
                    html.Tr([html.Td(label), html.Td(html.Code(str(value))), html.Td(_metric_meaning(label))])
                    for label, value in values
                ]
            ),
        ],
        bordered=True,
        hover=True,
        responsive=True,
        size="sm",
    )
    blocks.append(table)

    report_text = flow.summary_text("report", design)
    if report_text:
        blocks.extend([html.Hr(), html.Div("COLLECTED REPORT", className="fw-semibold mb-2"), _mono(report_text, "35vh")])
    else:
        blocks.append(_alert("No consolidated report yet. Run Generate Report after the stages you want to analyze.", "light"))

    report_paths = flow.report_paths(design)
    if report_paths and report_paths[1] and os.path.isfile(report_paths[1]):
        blocks.append(
            dbc.Button(
                [html.I(className="bi bi-box-arrow-up-right me-2"), "Open HTML report"],
                href=f"/results/report/{design}_report.html",
                target="_blank",
                color="info",
                outline=True,
                size="sm",
                className="mt-2",
            )
        )

    return html.Div(blocks)


def _metric_meaning(label):
    meanings = {
        "Verification": "Simulator/testbench result.",
        "Verification level": "Smoke vs user functional evidence.",
        "Functional correctness": "Only proven by passing assertion-based functional tests.",
        "Optimization analysis": "Whether optimization analysis produced data.",
        "Area": "Logic area from synthesis/analysis if available.",
        "Utilisation": "Physical core utilization if available.",
        "Total power": "Power estimate if the flow produced one.",
        "WNS": "Worst negative slack from timing analysis.",
        "TNS": "Total negative slack from timing analysis.",
        "Routing DRC errors": "Routing/physical rule violations if reported.",
    }
    return html.Span(meanings.get(label, "EDA result."), className="small text-secondary")


def _flatten_metrics(obj, prefix=""):
    result = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                result.update(_flatten_metrics(value, path))
            else:
                result[key.lower()] = value
                result[path.lower()] = value
    return result


def _first_value(metrics, keys):
    for key in keys:
        if key.lower() in metrics and metrics[key.lower()] not in (None, ""):
            return metrics[key.lower()]
    return None


def _optimization_tab(design):
    if design is None:
        return _alert("No design yet.", "info", "bi-info-circle")

    text = flow.summary_text("optimize", design)
    json_path = os.path.join(flow.RESULTS_DIR, "optimize", f"{design}_optimize.json")
    data = _safe_read_json(json_path)

    if not text and not data:
        return html.Div(
            [
                dbc.Alert(
                    [
                        html.Strong("Optimization is not AI optimization yet."),
                        html.Br(),
                        "The current optimizer is deterministic/rule-based analysis from RTL and EDA measurements. "
                        "That is intentional: AI will be added above these trusted results later.",
                    ],
                    color="info",
                    className="small",
                ),
                _alert(
                    "Run synthesis/P&R/STA first for richer PPA recommendations, then run Optimize Design.",
                    "light",
                    "bi-arrow-right",
                ),
            ]
        )

    blocks = [
        dbc.Alert(
            [
                html.Strong("Optimization analysis complete. "),
                "Recommendations are evidence-based engineering suggestions; they do not change RTL automatically and they do not prove correctness.",
            ],
            color="success",
            className="small",
        )
    ]

    if data:
        metrics = _flatten_metrics(data)
        evidence = []
        for label, keys in [
            ("Area", ("area", "total_area", "mapped_area")),
            ("Utilisation", ("utilisation", "utilization")),
            ("Power", ("total_power", "power")),
            ("WNS", ("wns", "worst_negative_slack")),
            ("TNS", ("tns", "total_negative_slack")),
            ("DRC", ("routing_drc_errors", "drc_errors")),
        ]:
            value = _first_value(metrics, keys)
            evidence.append(_metric_card(label, value if value is not None else "not measured", "info"))
        blocks.append(dbc.Row(evidence, className="g-2 mb-3"))

    if text:
        blocks.extend([html.Div("RECOMMENDATIONS", className="fw-semibold mb-2"), _mono(text)])
    else:
        blocks.append(_mono(json.dumps(data, indent=2)))

    return html.Div(blocks)


def _views(design):
    if design is None:
        return _alert("No design yet.", "info", "bi-info-circle")

    stamp = int(time.time())
    candidates = [
        (
            "Simulation waveform",
            "/results/design.png",
            os.path.join(flow.RESULTS_DIR, "design.png"),
            "Rendered from the VCD. Run Waveform PNG after simulation.",
        ),
        (
            "RTL schematic",
            f"/results/{design}.png",
            os.path.join(flow.RESULTS_DIR, f"{design}.png"),
            "Generated from Yosys/Graphviz. Run RTL Schematic.",
        ),
        (
            "Routed layout",
            f"/results/pnr/{design}_layout.png",
            os.path.join(flow.PNR_DIR, f"{design}_layout.png"),
            "Generated from the routed physical database. Run Place & Route.",
        ),
    ]

    blocks = [
        html.Div("VISUAL OUTPUTS", className="fw-semibold mb-2"),
        html.Div(
            "These images are views of real flow artifacts; a missing image means that stage has not produced it.",
            className="text-secondary small mb-3",
        ),
    ]
    for label, url, path, hint in candidates:
        if os.path.isfile(path):
            blocks.append(
                html.Figure(
                    [
                        html.Img(
                            src=f"{url}?t={stamp}",
                            className="img-fluid border rounded bg-white",
                        ),
                        html.Figcaption(label, className="form-text mt-1"),
                    ],
                    className="mb-4",
                )
            )
        else:
            blocks.append(
                html.Div(
                    [html.Div(label, className="fw-semibold small"), _alert(hint, "light")],
                    className="mb-3",
                )
            )
    return html.Div(blocks)


def _console(snapshot):
    if snapshot is None:
        return _alert("Nothing has run yet in this session.", "light")

    results = snapshot.get("results") or []
    rows = []
    for result in results:
        status = result.get("status", "unknown")
        colour = "success" if status == "ok" else "danger"
        rows.append(
            html.Tr(
                [
                    html.Td(result.get("label", result.get("stage", "-"))),
                    html.Td(dbc.Badge(status.upper(), color=colour)),
                    html.Td(f"{result.get('elapsed', 0.0):.1f}s", className="text-end small"),
                    html.Td(html.Span(result.get("error") or result.get("note") or "", className="small text-secondary")),
                ]
            )
        )

    blocks = []
    if rows:
        blocks.append(
            dbc.Table(
                [
                    html.Thead(html.Tr([html.Th("Stage"), html.Th("Result"), html.Th("Time", className="text-end"), html.Th("Detail")])),
                    html.Tbody(rows),
                ],
                size="sm",
                hover=True,
                responsive=True,
                className="mb-3",
            )
        )

    blocks.extend(
        [
            html.Div("COMMAND / TOOL OUTPUT", className="fw-semibold mb-2"),
            _mono("\n".join(snapshot.get("log") or []) or "(no output)"),
        ]
    )
    return html.Div(blocks)


def _job_banner(snapshot):
    if snapshot is None:
        return None

    status = snapshot["status"]
    colour = STATUS_COLOURS.get(status, "secondary")

    if status in ("running", "queued"):
        current = snapshot.get("current")
        label = flow.STAGES[current].label if current in flow.STAGES else "starting"
        done = len(snapshot.get("results") or [])
        total = len(snapshot.get("stages") or []) or 1
        return dbc.Alert(
            [
                dbc.Spinner(size="sm", color="primary", spinner_class_name="me-2"),
                html.Strong(f"{snapshot['title']} · {label}"),
                html.Span(f"  ({done}/{total} stages completed)", className="text-secondary"),
                dbc.Progress(
                    value=100 * done / total,
                    striped=True,
                    animated=True,
                    className="mt-2",
                    style={"height": "7px"},
                ),
            ],
            color="light",
            className="py-2 px-3 mb-0",
        )

    failed = [r.get("label", r.get("stage")) for r in snapshot.get("results", []) if r.get("status") != "ok"]
    if status == "ok":
        message = f"{snapshot['title']}: requested stages completed."
        icon = "bi-check-circle-fill"
    elif status == "partial":
        message = f"{snapshot['title']}: completed with {len(failed)} non-fatal failure(s): {', '.join(failed)}."
        icon = "bi-exclamation-triangle-fill"
    else:
        message = f"{snapshot['title']}: stopped after a failure. See Console for the actual tool error."
        icon = "bi-x-circle-fill"
    return dbc.Alert(
        [html.I(className=f"bi {icon} me-2"), message],
        color=colour,
        className="py-2 px-3 mb-0",
    )


def _tool_strip():
    parts = []
    for entry in flow.tool_availability():
        colour = "success" if entry["available"] else "secondary"
        parts.append(
            dbc.Badge(
                entry["tool"],
                color=colour,
                className="me-1",
                title=f"{entry['used_for']} — {'found' if entry['available'] else 'not on PATH'}",
            )
        )
    return html.Div([html.Span("EDA tools: ", className="me-1")] + parts)


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------


def _collect_overrides(tb_lang, clk_period, core_util, place_density, tb_cycles):
    return {
        "TB_LANG": tb_lang or "python",
        **({"CLK_PERIOD": clk_period} if clk_period not in (None, "") else {}),
        **({"CORE_UTIL": int(core_util)} if core_util not in (None, "") else {}),
        **({"PLACE_DENSITY": place_density} if place_density not in (None, "") else {}),
        **({"TB_CYCLES": int(tb_cycles)} if tb_cycles not in (None, "") else {}),
    }


def _register_callbacks(app):

    @app.callback(
        Output("settings", "data"),
        Input("tb-lang", "value"),
        Input("clk-period", "value"),
        Input("core-util", "value"),
        Input("place-density", "value"),
        Input("tb-cycles", "value"),
    )
    def _store_settings(tb_lang, clk_period, core_util, place_density, tb_cycles):
        return _collect_overrides(tb_lang, clk_period, core_util, place_density, tb_cycles)

    @app.callback(
        Output("design-select", "options"),
        Output("design-select", "value"),
        Output("design-note", "children"),
        Output("design-badge", "children"),
        Input("btn-create", "n_clicks"),
        Input("upload-design", "contents"),
        Input("poll", "n_intervals"),
        State("design-select", "value"),
        prevent_initial_call=False,
    )
    def _refresh_designs(_clicks, _contents, _ticks, selected):
        designs = flow.list_designs()
        options = [{"label": name, "value": name} for name in designs]
        current = flow.current_design()
        value = current if current in designs else (selected if selected in designs else (designs[0] if designs else None))
        if not designs:
            return options, None, "src/ is empty.", dbc.Badge("no design", color="warning")
        note = f"{len(designs)} design(s) in src/. Active flow design: {value}."
        return options, value, note, dbc.Badge(value, color="light", text_color="dark")

    @app.callback(
        Output("upload-result", "children", allow_duplicate=True),
        Input("btn-create", "n_clicks"),
        State("design-name", "value"),
        prevent_initial_call=True,
    )
    def _create_design(_clicks, name):
        try:
            path = flow.generate_design(name)
        except flow.FlowError as error:
            return _alert(str(error), "danger", "bi-x-circle")
        return _alert(f"Created {path}. It is now available in the design selector.", "success", "bi-check-circle")

    @app.callback(
        Output("upload-result", "children", allow_duplicate=True),
        Input("upload-design", "contents"),
        State("upload-design", "filename"),
        prevent_initial_call=True,
    )
    def _upload(contents, filenames):
        if not contents:
            return no_update
        if isinstance(contents, str):
            contents, filenames = [contents], [filenames]
        written, failed = [], []
        for content, filename in zip(contents, filenames or []):
            try:
                _, _, payload = content.partition(",")
                data = base64.b64decode(payload)
                written.extend(flow.save_upload(filename, data))
            except flow.FlowError as error:
                failed.append(str(error))
            except Exception as error:  # noqa: BLE001
                failed.append(f"{filename}: {error}")

        blocks = []
        if written:
            blocks.append(
                dbc.ListGroup(
                    [
                        dbc.ListGroupItem(
                            [html.Code(path), html.Span(f" — {note}", className="small text-secondary")],
                            className="py-1 px-2 small",
                        )
                        for path, note in written
                    ],
                    flush=True,
                    className="mb-2",
                )
            )
        for message in failed:
            blocks.append(_alert(message, "danger", "bi-x-circle"))
        if written and not failed:
            blocks.append(
                _alert(
                    "Upload complete. Open Pipeline to inspect the parsed top module and ports before running the flow.",
                    "success",
                    "bi-check-circle",
                )
            )
        return html.Div(blocks)

    @app.callback(
        Output("job-id", "data"),
        Output("poll", "disabled"),
        Output("tabs", "active_tab"),
        Input({"role": "stage", "stage": ALL}, "n_clicks"),
        Input("btn-all", "n_clicks"),
        State("settings", "data"),
        State("all-scope", "value"),
        prevent_initial_call=True,
    )
    def _start(_stage_clicks, _all_clicks, settings, scope):
        triggered = ctx.triggered_id
        if triggered is None or not ctx.triggered:
            return no_update, no_update, no_update
        value = ctx.triggered[0].get("value")
        if not value:
            return no_update, no_update, no_update

        if triggered == "btn-all":
            include_backend = "backend" in (scope or [])
            stages = flow.ALL_STAGES if include_backend else flow.FRONTEND_STAGES
            title = "Full RTL → GDSII Flow" if include_backend else "RTL Front-End Flow"
            active_tab = "tab-console"
        else:
            key = triggered["stage"]
            stages = [key]
            title = flow.STAGES[key].label
            active_tab = "tab-optimize" if key == "optimize" else "tab-report" if key == "report" else "tab-console"

        try:
            job_id = flow.start_job(stages, title=title, overrides=settings or {"TB_LANG": "python"})
        except flow.FlowError as error:
            # There is no dedicated error output in this callback. The banner
            # will remain unchanged; the console is therefore the safest place
            # to surface asynchronous errors from the flow runner.
            return no_update, no_update, no_update
        return job_id, False, active_tab

    @app.callback(
        Output("job-banner", "children"),
        Output("poll", "disabled", allow_duplicate=True),
        Input("poll", "n_intervals"),
        Input("job-id", "data"),
        prevent_initial_call=True,
    )
    def _progress(_ticks, job_id):
        snapshot = flow.get_job(job_id) if job_id else None
        if snapshot is None:
            return None, True
        active = snapshot["status"] in ("queued", "running")
        return _job_banner(snapshot), not active

    @app.callback(
        Output("tab-content", "children"),
        Input("tabs", "active_tab"),
        Input("job-banner", "children"),
        Input("design-select", "value"),
        Input("upload-result", "children"),
        State("job-id", "data"),
        State("settings", "data"),
    )
    def _render_tab(active_tab, _banner, design, _upload, job_id, settings):
        design = design or flow.current_design()
        snapshot = flow.get_job(job_id) if job_id else flow.latest_job()

        if active_tab == "tab-overview":
            return _pipeline_view(design, settings, snapshot)
        if active_tab == "tab-report":
            return _metrics_tab(design)
        if active_tab == "tab-optimize":
            return _optimization_tab(design)
        if active_tab == "tab-views":
            return _views(design)
        return _console(snapshot)

    @app.callback(
        Output("gtkwave-result", "children"),
        Input("btn-gtkwave", "n_clicks"),
        prevent_initial_call=True,
    )
    def _gtkwave(_clicks):
        try:
            message = flow.launch_gtkwave()
        except flow.FlowError as error:
            return _alert(str(error), "warning", "bi-exclamation-triangle")
        return _alert(message, "success", "bi-check-circle")

    @app.callback(
        Output("tool-strip", "children"),
        Input("tabs", "active_tab"),
    )
    def _tools(_tab):
        return _tool_strip()
