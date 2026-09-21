#!/usr/bin/env python3
"""Collect every artefact the flow produced into one report.

Each stage already writes its own log and summary, scattered across results/
and results/pnr/. This gathers them into a single document that says what was
built, which stages ran, and what they measured -- the thing you would attach
to a review or keep alongside a tapeout candidate.

Two formats are written from the same data: plain text, which the dashboard
shows inline and which diffs cleanly between runs, and HTML, which keeps the
generated PNGs (waveform, schematic, routed layout) next to the numbers.

Nothing is inferred here. A stage that did not run is reported as not run,
and a number no tool emitted is reported as missing, because a report that
quietly fills in blanks is worse than no report.

Usage:
  gen_report.py <design_name> [--src DIR] [--tb DIR] [--results DIR]
                              [--pnr-dir DIR] [--out-dir DIR] [--period NS]
"""
import argparse
import html
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rtl_parse import parse_sources  # noqa: E402


def read_text(path):
    if path and os.path.isfile(path):
        with open(path, errors="replace") as handle:
            return handle.read()
    return None


def file_info(path):
    """Size and modification time for an artefact, or None if it is absent."""
    if not path or not os.path.isfile(path):
        return None
    stat = os.stat(path)
    return {
        "path": path,
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
    }


def size_label(count):
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.0f} B"


# ---------------------------------------------------------------------------
# Gathering
# ---------------------------------------------------------------------------

def collect_artifacts(design, src_dir, tb_dir, results_dir, pnr_dir):
    """Every file the flow is expected to produce, with whether it exists."""
    return [
        ("RTL design", os.path.join(src_dir, f"{design}.sv")),
        ("SystemVerilog testbench", os.path.join(tb_dir, f"test_{design}.sv")),
        ("cocotb testbench", os.path.join(tb_dir, f"test_{design}.py")),
        ("Simulation waveform (VCD)", os.path.join(results_dir, "design.vcd")),
        ("Waveform PNG", os.path.join(results_dir, "design.png")),
        ("Generic netlist", os.path.join(results_dir, "netlist.v")),
        ("RTL schematic (dot)", os.path.join(results_dir, f"{design}_rtl.dot")),
        ("RTL schematic PNG", os.path.join(results_dir, f"{design}.png")),
        ("Timing constraints (SDC)", os.path.join(pnr_dir, f"{design}.sdc")),
        ("Mapped netlist", os.path.join(pnr_dir, f"{design}_mapped.v")),
        ("Routed DEF", os.path.join(pnr_dir, f"{design}_routed.def")),
        ("Routed database (ODB)", os.path.join(pnr_dir, f"{design}.odb")),
        ("Extracted parasitics (SPEF)", os.path.join(pnr_dir, f"{design}.spef")),
        ("Layout PNG", os.path.join(pnr_dir, f"{design}_layout.png")),
        ("P&R summary", os.path.join(pnr_dir, f"{design}_summary.txt")),
        ("STA summary", os.path.join(pnr_dir, f"{design}_sta_summary.txt")),
        ("Signoff summary", os.path.join(pnr_dir, f"{design}_signoff_summary.txt")),
        ("Optimisation analysis",
         os.path.join(results_dir, "optimize", f"{design}_optimize.txt")),
        ("AI engineering report",
         os.path.join(results_dir, "report", f"{design}_ai_report.txt")),
    ]


def collect_stages(design, results_dir, pnr_dir):
    """Stage name -> (ran?, summary text or note)."""
    vcd = os.path.join(results_dir, "design.vcd")
    netlist = os.path.join(results_dir, "netlist.v")
    schematic = os.path.join(results_dir, f"{design}.png")

    synth_log = read_text(os.path.join(pnr_dir, f"{design}_synth.log"))
    stages = []

    stages.append((
        "Simulation", os.path.isfile(vcd),
        f"VCD written ({size_label(os.path.getsize(vcd))})"
        if os.path.isfile(vcd) else "no VCD produced",
    ))
    stages.append((
        "Synthesis (generic)", os.path.isfile(netlist),
        f"netlist written ({size_label(os.path.getsize(netlist))})"
        if os.path.isfile(netlist) else "not run",
    ))
    stages.append((
        "RTL schematic", os.path.isfile(schematic),
        "PNG rendered" if os.path.isfile(schematic) else "not run",
    ))

    mapped = summarize_synth_log(synth_log)
    stages.append((
        "Technology mapping", synth_log is not None,
        mapped or ("not run" if synth_log is None else "log present, no stat block"),
    ))

    for label, suffix in (("Place and route", "_summary.txt"),
                          ("Static timing analysis", "_sta_summary.txt"),
                          ("Physical signoff", "_signoff_summary.txt")):
        text = read_text(os.path.join(pnr_dir, f"{design}{suffix}"))
        verdict = None
        if text:
            match = re.search(r"^Result:\s*(.+)$", text, re.M)
            verdict = match.group(1).strip() if match else "completed"
        stages.append((label, text is not None, verdict or "not run"))

    optimize = read_text(os.path.join(results_dir, "optimize",
                                      f"{design}_optimize.txt"))
    tip_count = len(re.findall(r"^  \d+\. \[", optimize, re.M)) if optimize else 0
    stages.append((
        "Optimisation analysis", optimize is not None,
        f"{tip_count} recommendation(s)" if optimize else "not run",
    ))
    ai_report = read_text(os.path.join(results_dir, "report", f"{design}_ai_report.txt"))
    stages.append((
        "AI engineering analysis", ai_report is not None,
        "Ollama-backed evidence analysis" if ai_report else "not run",
    ))
    return stages


def summarize_synth_log(text):
    """One-line cell/area result from the technology-mapping log."""
    if not text:
        return None
    cells = re.findall(r"Number of cells:\s+(\d+)", text)
    area = re.findall(r"Chip area for (?:top )?module '[^']*':\s*([\d.]+)", text)
    if cells and area:
        return f"{cells[-1]} standard cells, {float(area[-1]):.1f} um^2"
    if cells:
        return f"{cells[-1]} cells"
    return None


def collect_summaries(design, results_dir, pnr_dir):
    """The full text of each stage summary, in flow order."""
    named = [
        ("Place and route", os.path.join(pnr_dir, f"{design}_summary.txt")),
        ("Static timing analysis",
         os.path.join(pnr_dir, f"{design}_sta_summary.txt")),
        ("Physical signoff",
         os.path.join(pnr_dir, f"{design}_signoff_summary.txt")),
        ("Optimisation analysis",
         os.path.join(results_dir, "optimize", f"{design}_optimize.txt")),
        ("AI engineering report",
         os.path.join(results_dir, "report", f"{design}_ai_report.txt")),
    ]
    return [(label, read_text(path)) for label, path in named]


def describe_interface(rtl_path, design):
    """Port table for the design, or None when the RTL cannot be read."""
    if not os.path.isfile(rtl_path):
        return None
    parsed = parse_sources([rtl_path], top=design)
    module = parsed["top_module"]
    if module is None:
        return None
    return {
        "top": module["name"],
        "params": module["params"],
        "modules": [m["name"] for m in parsed["modules"]],
        "ports": [
            {
                "name": port["name"],
                "direction": port["direction"],
                "width": port["width"],
                "range": (f"[{port['msb']}:{port['lsb']}]"
                          if port["vector"] else ""),
                "role": ("clock" if port["is_clock"] else
                         "reset (active low)" if port["is_reset"] and port["active_low"]
                         else "reset" if port["is_reset"] else ""),
            }
            for port in module["ports"]
        ],
    }


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def render_text(design, interface, stages, artifacts, summaries, period):
    title = f"VeriOpt flow report: {design}"
    lines = [
        title, "=" * len(title), "",
        f"Generated  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"Design     {design}",
        f"Period     {period} ns",
        "",
    ]

    # ---- interface ----
    lines += ["Interface", "-" * 9, ""]
    if interface:
        lines.append(f"  top module   {interface['top']}")
        if len(interface["modules"]) > 1:
            lines.append(f"  modules      {', '.join(interface['modules'])}")
        if interface["params"]:
            for name, value in interface["params"].items():
                lines.append(f"  parameter    {name} = {value}")
        lines.append("")
        lines.append(f"  {'port':<20} {'dir':<7} {'width':<7} {'range':<14} role")
        lines.append(f"  {'-'*20} {'-'*7} {'-'*7} {'-'*14} {'-'*18}")
        for port in interface["ports"]:
            width = str(port["width"]) if port["width"] is not None else "?"
            lines.append(f"  {port['name']:<20} {port['direction']:<7} "
                         f"{width:<7} {port['range']:<14} {port['role']}")
    else:
        lines.append("  RTL source not found; interface could not be read.")
    lines.append("")

    # ---- stages ----
    lines += ["Stages", "-" * 6, ""]
    for label, ran, note in stages:
        mark = "ok " if ran else "-- "
        lines.append(f"  [{mark}] {label:<26} {note}")
    lines.append("")

    # ---- artifacts ----
    lines += ["Artefacts", "-" * 9, ""]
    for label, path in artifacts:
        info = file_info(path)
        if info:
            lines.append(f"  [ok ] {label:<28} {path}")
            lines.append(f"         {size_label(info['size']):>9}   {info['modified']}")
        else:
            lines.append(f"  [-- ] {label:<28} {path} (absent)")
    lines.append("")

    # ---- summaries ----
    lines += ["Stage summaries", "-" * 15, ""]
    any_summary = False
    for label, text in summaries:
        if not text:
            continue
        any_summary = True
        lines.append(f"### {label}")
        lines.append("")
        lines += [f"  {line}" for line in text.rstrip().splitlines()]
        lines.append("")
    if not any_summary:
        lines.append("  No stage summaries yet. Run place and route, STA and signoff.")
        lines.append("")

    # ---- AI engineering analysis ----
    ai_path = next((path for label, path in artifacts
                    if label == "AI engineering report"), None)
    ai_text = read_text(ai_path) if ai_path else None
    if ai_text:
        lines += ["AI engineering analysis", "-" * 23, "", ai_text.rstrip(), ""]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0; background: #f4f5f7; color: #1c2430; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 16px; text-transform: uppercase; letter-spacing: .06em;
     color: #5a6473; margin: 32px 0 10px; padding-bottom: 6px;
     border-bottom: 1px solid #dde1e7; }
h3 { font-size: 14px; margin: 20px 0 6px; }
.meta { color: #5a6473; font-size: 13px; margin-bottom: 4px; }
table { border-collapse: collapse; width: 100%; font-size: 13px;
        background: #fff; border: 1px solid #dde1e7; border-radius: 6px;
        overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #eceef2; }
th { background: #fafbfc; font-weight: 600; color: #46505f; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre { background: #fff; border: 1px solid #dde1e7; border-radius: 6px;
      padding: 12px 14px; font-size: 12.5px; line-height: 1.5;
      overflow-x: auto; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px;
         font-size: 11.5px; font-weight: 600; }
.ok   { background: #e3f5e9; color: #1c6b3c; }
.miss { background: #eef0f3; color: #6b7480; }
figure { margin: 0 0 20px; }
figure img { max-width: 100%; border: 1px solid #dde1e7; border-radius: 6px;
             background: #fff; }
figcaption { font-size: 12px; color: #5a6473; margin-top: 5px; }
"""


def badge(ran):
    return ('<span class="badge ok">present</span>' if ran
            else '<span class="badge miss">absent</span>')


def render_html(design, interface, stages, artifacts, summaries, period, images):
    esc = html.escape
    out = [
        f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        f"<title>VeriOpt report: {esc(design)}</title>",
        f"<style>{CSS}</style></head><body><div class=\"wrap\">",
        f"<h1>VeriOpt flow report</h1>",
        f"<p class=\"meta\">Design <code>{esc(design)}</code> &middot; target period "
        f"{esc(str(period))} ns &middot; generated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</p>",
    ]

    # ---- interface ----
    out.append("<h2>Interface</h2>")
    if interface:
        if interface["params"]:
            params = ", ".join(f"{esc(k)} = {esc(str(v))}"
                               for k, v in interface["params"].items())
            out.append(f"<p class=\"meta\">Top <code>{esc(interface['top'])}</code>"
                       f" &middot; parameters: {params}</p>")
        out.append("<table><thead><tr><th>Port</th><th>Direction</th>"
                   "<th>Width</th><th>Range</th><th>Role</th></tr></thead><tbody>")
        for port in interface["ports"]:
            width = port["width"] if port["width"] is not None else "?"
            out.append(
                f"<tr><td><code>{esc(port['name'])}</code></td>"
                f"<td>{esc(port['direction'])}</td>"
                f"<td class=\"num\">{esc(str(width))}</td>"
                f"<td><code>{esc(port['range'])}</code></td>"
                f"<td>{esc(port['role'])}</td></tr>"
            )
        out.append("</tbody></table>")
    else:
        out.append("<p>RTL source not found; interface could not be read.</p>")

    # ---- stages ----
    out.append("<h2>Stages</h2><table><thead><tr><th>Stage</th><th>Status</th>"
               "<th>Result</th></tr></thead><tbody>")
    for label, ran, note in stages:
        out.append(f"<tr><td>{esc(label)}</td><td>{badge(ran)}</td>"
                   f"<td>{esc(note)}</td></tr>")
    out.append("</tbody></table>")

    # ---- images ----
    present = [(label, url) for label, url, path in images if os.path.isfile(path)]
    if present:
        out.append("<h2>Generated views</h2>")
        for label, url in present:
            out.append(f"<figure><img src=\"{esc(url)}\" alt=\"{esc(label)}\">"
                       f"<figcaption>{esc(label)}</figcaption></figure>")

    # ---- artifacts ----
    out.append("<h2>Artefacts</h2><table><thead><tr><th>Artefact</th><th>Path</th>"
               "<th>Size</th><th>Modified</th></tr></thead><tbody>")
    for label, path in artifacts:
        info = file_info(path)
        if info:
            out.append(f"<tr><td>{esc(label)}</td><td><code>{esc(path)}</code></td>"
                       f"<td class=\"num\">{esc(size_label(info['size']))}</td>"
                       f"<td>{esc(info['modified'])}</td></tr>")
        else:
            out.append(f"<tr><td>{esc(label)}</td><td><code>{esc(path)}</code></td>"
                       f"<td colspan=\"2\">{badge(False)}</td></tr>")
    out.append("</tbody></table>")

    # ---- summaries ----
    out.append("<h2>Stage summaries</h2>")
    if any(text for _, text in summaries):
        for label, text in summaries:
            if text:
                out.append(f"<h3>{esc(label)}</h3><pre>{esc(text.rstrip())}</pre>")
    else:
        out.append("<p>No stage summaries yet. Run place and route, STA and signoff.</p>")

    ai_path = os.path.join(args.results, "report", f"{args.design}_ai_report.txt") if False else None
    # The AI report lives beside the consolidated report. Include it only when present.
    ai_report_path = os.path.join(os.path.dirname(os.path.join("x", "x")), "x") if False else None
    out_ai = None
    # render_html has no args namespace; derive the report directory from the first image/report path convention.
    # The caller's results directory is not otherwise passed, so use the conventional sibling path from artifacts.
    for label, path in artifacts:
        if label == "AI engineering report":
            out_ai = path
            break
    if out_ai:
        ai_text = read_text(out_ai)
        if ai_text:
            out.append("<h2>AI engineering analysis</h2><pre>" + esc(ai_text.rstrip()) + "</pre>")

    out.append("</div></body></html>")
    return "\n".join(out)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("design")
    parser.add_argument("--src", default="src")
    parser.add_argument("--tb", default="tb")
    parser.add_argument("--results", default="results")
    parser.add_argument("--pnr-dir", default=None)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--period", default="10.0")
    args = parser.parse_args()

    pnr_dir = args.pnr_dir or os.path.join(args.results, "pnr")
    out_dir = args.out_dir or os.path.join(args.results, "report")

    rtl_path = os.path.join(args.src, f"{args.design}.sv")
    interface = describe_interface(rtl_path, args.design)
    stages = collect_stages(args.design, args.results, pnr_dir)
    artifacts = collect_artifacts(args.design, args.src, args.tb,
                                  args.results, pnr_dir)
    summaries = collect_summaries(args.design, args.results, pnr_dir)

    # The HTML report is served from results/report/, and the dashboard serves
    # results/ at /results/, so images are referenced by that URL rather than a
    # relative path -- a relative path would break as soon as the file is
    # opened through the web server instead of from disk.
    images = [
        ("Simulation waveform", "/results/design.png",
         os.path.join(args.results, "design.png")),
        ("RTL schematic", f"/results/{args.design}.png",
         os.path.join(args.results, f"{args.design}.png")),
        ("Routed layout", f"/results/pnr/{args.design}_layout.png",
         os.path.join(pnr_dir, f"{args.design}_layout.png")),
    ]

    text = render_text(args.design, interface, stages, artifacts, summaries,
                       args.period)
    page = render_html(args.design, interface, stages, artifacts, summaries,
                       args.period, images)

    os.makedirs(out_dir, exist_ok=True)
    text_path = os.path.join(out_dir, f"{args.design}_report.txt")
    html_path = os.path.join(out_dir, f"{args.design}_report.html")
    with open(text_path, "w") as handle:
        handle.write(text)
    with open(html_path, "w") as handle:
        handle.write(page)

    print(text)
    print(f"Wrote {text_path}")
    print(f"Wrote {html_path}")


if __name__ == "__main__":
    main()
