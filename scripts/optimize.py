#!/usr/bin/env python3
"""Analyse a design and report ranked, evidence-backed optimisation advice.

This replaces scripts/profile.py, which counted regex hits in a netlist,
looked for a "Critical Path:" line that OpenSTA does not emit, and then
shelled out to `ollama run llama2:7b --prompt ...` -- a flag ollama does not
accept. It could not have produced a usable result.

The rule here is that every number in the output is measured, not guessed:

  * structure comes from Yosys `stat` and `ltp` on the real RTL -- cell mix,
    flip-flop and mux counts, and the longest topological path, which is the
    logic depth that actually sets the achievable clock period
  * area comes from `stat -liberty` against the PDK, in real um^2
  * the area/delay trade-off is *measured* by mapping the design twice, once
    timing-driven and once unconstrained, and comparing the results
  * timing comes from whatever the STA and signoff stages already wrote

Where a number is not available -- no PDK, no prior place and route -- the
corresponding advice is omitted rather than invented.

Usage:
  optimize.py <rtl.sv> <design_name> [--period NS] [--lib LIBERTY]
              [--pnr-dir DIR] [--out-dir DIR] [--db veriopt.db]
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rtl_parse import parse_sources  # noqa: E402

YOSYS_TIMEOUT = 600

# Logic depth past which a single-cycle path is worth questioning. Not a hard
# rule -- it is the point where the advice below stops being noise for the
# 10 ns default period, and it is reported alongside the measured number so
# the reader can judge it against their own target.
DEEP_LOGIC_LEVELS = 20

# Utilisation above this tends to make detailed routing struggle on sky130hd.
HIGH_UTILISATION_PCT = 70.0


# ---------------------------------------------------------------------------
# Tool plumbing
# ---------------------------------------------------------------------------

def run_yosys(script, label):
    """Run a Yosys script, returning (stdout, error_or_None)."""
    if not shutil.which("yosys"):
        return "", "yosys not found on PATH"
    try:
        result = subprocess.run(
            ["yosys", "-p", script],
            capture_output=True, text=True, timeout=YOSYS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "", f"{label}: yosys timed out after {YOSYS_TIMEOUT}s"
    if result.returncode != 0:
        tail = [ln for ln in (result.stdout + result.stderr).splitlines() if ln.strip()]
        return result.stdout, f"{label}: {' | '.join(tail[-3:]) or 'yosys failed'}"
    return result.stdout, None


def strip_dump_block(rtl_path):
    """Write a synthesis-safe copy of the RTL to a temp file, return its path.

    Mirrors scripts/strip_dumpblock.py: the $dumpfile/$dumpvars block is
    simulation-only and Yosys rejects it.
    """
    with open(rtl_path, errors="replace") as handle:
        text = handle.read()
    text = re.sub(
        r"initial\s*begin\s*\$dumpfile\([^)]*\)\s*;\s*\$dumpvars\([^)]*\)\s*;\s*end",
        "", text, flags=re.IGNORECASE | re.DOTALL,
    )
    handle = tempfile.NamedTemporaryFile("w", suffix=".sv", delete=False)
    handle.write(text)
    handle.close()
    return handle.name


# ---------------------------------------------------------------------------
# Yosys output parsing
# ---------------------------------------------------------------------------

def parse_stat(text):
    """Pull totals and the per-cell-type histogram out of `stat` output."""
    stats = {"cells": None, "wires": None, "area": None, "cell_types": {}}

    cells = re.findall(r"Number of cells:\s+(\d+)", text)
    if cells:
        stats["cells"] = int(cells[-1])
    wires = re.findall(r"Number of wires:\s+(\d+)", text)
    if wires:
        stats["wires"] = int(wires[-1])
    area = re.findall(r"Chip area for (?:top )?module '[^']*':\s*([\d.]+)", text)
    if area:
        stats["area"] = float(area[-1])

    # The histogram is the indented "  <cellname>  <count>" block that follows
    # the "Number of cells:" line. Taking the last block matters: `stat` runs
    # per module, and the flattened top is printed last.
    block = text.rpartition("Number of cells:")[2]
    for cell, count in re.findall(r"^\s{4,}(\S+)\s+(\d+)\s*$", block, re.M):
        stats["cell_types"][cell] = int(count)
    return stats


def parse_ltp(text):
    """Longest topological path length, i.e. logic levels between registers."""
    matches = re.findall(r"Longest topological path in .*?\(length=(\d+)\)", text)
    return int(matches[-1]) if matches else None


def cell_root(cell):
    """Strip the `$` of a generic cell or the library prefix of a real one.

    `$dff` and `sky130_fd_sc_hd__dfrtp_1` describe the same thing, and the
    advice below has to count both. Everything before the `__` is the library
    name, which says nothing about the cell's function.
    """
    name = cell.lower().lstrip("$\\")
    if "__" in name:
        name = name.split("__", 1)[1]
    return name


# Sequential cells, generic and sky130hd alike. Written as patterns on the
# cell root because a substring test for "dff" is wrong in both directions:
# it misses every real sky130 flop -- they are named dfrtp, dfxtp, dfstp,
# sdfrtp, edfxtp, none of which contains "dff" -- while a looser test for
# "df"/"dl" would wrongly claim dlclkp (a clock gate) and dlygate (a delay
# cell) as registers.
FLOP_PATTERNS = (
    re.compile(r"^(?:a|s|e|as)?d(?:ff|f[a-z])"),   # $dff, $adff, dfrtp, sdfxtp, edfxtp
    re.compile(r"^a?dlatch"),                       # $dlatch, $adlatch
    re.compile(r"^dl[xr][tb][pn]"),                 # dlxtp, dlrtp, dlxbp, ...
)

# Arithmetic primitives: generic operators, plus sky130's full/half adders
# and majority gate, which is how an adder actually appears after mapping.
ARITH_PATTERNS = (
    re.compile(r"^(?:add|sub|mul|div|mod|alu|shift|shl|shr|neg|abs)"),
    re.compile(r"^(?:lt|le|gt|ge|eq|ne)$"),
    re.compile(r"^(?:fa|ha|maj)"),
)


def classify_cells(cell_types):
    """Group a cell histogram into the categories the advice keys off."""
    groups = {"flipflops": 0, "muxes": 0, "arith": 0, "memory": 0, "logic": 0}
    for cell, count in cell_types.items():
        name = cell_root(cell)
        if any(pattern.match(name) for pattern in FLOP_PATTERNS):
            groups["flipflops"] += count
        elif "mux" in name:
            groups["muxes"] += count
        elif any(pattern.match(name) for pattern in ARITH_PATTERNS):
            groups["arith"] += count
        elif name.startswith("mem") or "mem" in name:
            groups["memory"] += count
        else:
            groups["logic"] += count
    return groups


# ---------------------------------------------------------------------------
# Reading back what earlier stages measured
# ---------------------------------------------------------------------------

def read_text(path):
    if path and os.path.exists(path):
        with open(path, errors="replace") as handle:
            return handle.read()
    return None


def number_from(text, pattern):
    if not text:
        return None
    match = re.search(pattern, text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_prior_stages(pnr_dir, design):
    """Collect the numbers the pnr / sta / signoff summaries already contain.

    These stages are the authority on timing and area -- re-deriving any of it
    here would risk disagreeing with the reports the user is looking at.

    The SDC is read too, for the clock period those numbers were measured
    against. Without it a slack measured at 10 ns gets compared against
    whatever --period this run was given, which produces claims that cannot
    be true -- a slack larger than the period it supposedly has.
    """
    sta = read_text(os.path.join(pnr_dir, f"{design}_sta_summary.txt"))
    pnr = read_text(os.path.join(pnr_dir, f"{design}_summary.txt"))
    signoff = read_text(os.path.join(pnr_dir, f"{design}_signoff_summary.txt"))
    sdc = read_text(os.path.join(pnr_dir, f"{design}.sdc"))
    source = sta or pnr

    return {
        "sdc_period": number_from(sdc, r"create_clock[^\n]*-period\s+([\d.]+)"),
        "has_sta": sta is not None,
        "has_pnr": pnr is not None,
        "has_signoff": signoff is not None,
        "wns": number_from(source, r"WNS\s+(-?[\d.]+)"),
        "setup_slack": number_from(source, r"worst setup slack\s+(-?[\d.]+)"),
        "hold_slack": number_from(source, r"worst hold slack\s+(-?[\d.]+)"),
        "fmax_mhz": number_from(sta, r"max clock frequency\s+([\d.]+)"),
        "min_period": number_from(sta, r"min clock period\s+([\d.]+)"),
        "utilisation": number_from(signoff or pnr, r"utilization\s+([\d.]+)%"),
        "die": (re.search(r"die\s+([\d.]+ x [\d.]+ um)", signoff or pnr or "")
                or [None, None])[1] if (signoff or pnr) else None,
        "drc": number_from(signoff or pnr, r"routing DRC errors\s+(\d+)"),
        "power_w": number_from(signoff or sta or pnr, r"total power\s+([\d.eE+-]+)"),
        "drv": number_from(signoff or sta, r"slew/cap/fanout violations\s+(\d+)"),
    }


# ---------------------------------------------------------------------------
# The measurements
# ---------------------------------------------------------------------------

def measure_generic(rtl, design):
    """Technology-independent structure: cell mix and logic depth."""
    script = "; ".join([
        f"read_verilog -sv {rtl}",
        f"hierarchy -check -top {design}",
        "proc", "opt", "fsm", "opt", "memory -nomap", "opt",
        "stat", "ltp",
    ])
    text, error = run_yosys(script, "generic analysis")
    stats = parse_stat(text)
    return {
        "error": error,
        "cells": stats["cells"],
        "wires": stats["wires"],
        "cell_types": stats["cell_types"],
        "groups": classify_cells(stats["cell_types"]),
        "logic_levels": parse_ltp(text),
    }


def measure_mapped(rtl, design, lib, period_ns, timing_driven):
    """Map to the PDK and report real cell count and area in um^2.

    Run twice by the caller -- once timing-driven (`abc -D`), once with abc
    left to minimise on its own -- so the area cost of the timing target is
    a measured delta rather than an assertion.
    """
    abc = f"abc -liberty {lib}"
    if timing_driven:
        abc += f" -D {int(period_ns * 1000)}"
    script = "; ".join([
        f"read_verilog -sv {rtl}",
        f"hierarchy -check -top {design}",
        f"synth -top {design} -flatten",
        f"dfflibmap -liberty {lib}",
        abc,
        "setundef -zero", "splitnets", "opt_clean -purge",
        f"stat -liberty {lib}",
    ])
    label = "timing-driven mapping" if timing_driven else "area mapping"
    text, error = run_yosys(script, label)
    stats = parse_stat(text)
    return {
        "error": error,
        "cells": stats["cells"],
        "area_um2": stats["area"],
        "cell_types": stats["cell_types"],
        "groups": classify_cells(stats["cell_types"]),
    }


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------

def advise(module, generic, timed, area, prior, period_ns):
    """Return a list of {severity, title, detail}, most important first.

    Each entry cites the measurement it came from. Anything that would need a
    number we do not have is simply not emitted.
    """
    tips = []

    def add(severity, title, detail):
        tips.append({"severity": severity, "title": title, "detail": detail})

    groups = generic.get("groups") or {}
    levels = generic.get("logic_levels")

    # Two different numbers, and using one for the other's job is why this
    # used to go quiet on a design with plenty of headroom. WNS is *worst
    # negative* slack: OpenSTA reports 0 when nothing violates, so it detects
    # failure but says nothing about margin. `worst setup slack` is the real
    # slack on the critical path and is what margin has to be read from.
    wns = prior.get("wns")
    slack = prior.get("setup_slack")
    if slack is None:
        slack = wns
    violating = (wns is not None and wns < 0) or (slack is not None and slack < 0)

    # Slack is only meaningful against the period it was measured at. If this
    # run was given a different --period than the last place-and-route used,
    # the measured numbers still describe that older constraint, so the older
    # period is what the margin has to be computed from -- and the mismatch is
    # worth saying out loud, because the advice is then about a run the user
    # may not have meant.
    timing_period = prior.get("sdc_period") or period_ns
    stale_period = (prior.get("sdc_period") is not None
                    and abs(prior["sdc_period"] - period_ns) > 1e-6)

    # ---- timing ----
    if violating:
        worst = wns if (wns is not None and wns < 0) else slack
        detail = (f"Worst negative slack is {worst:.3f} ns against a "
                  f"{timing_period} ns period, so the design does not "
                  f"currently meet timing.")
        if levels:
            detail += (f" The longest register-to-register path is {levels} logic "
                       f"levels deep; splitting it with a pipeline register is the "
                       f"structural fix.")
        if prior.get("min_period"):
            detail += (f" Post-route STA measured a minimum period of "
                       f"{prior['min_period']} ns, so CLK_PERIOD={prior['min_period']} "
                       f"is the achievable target without RTL changes.")
        add("high", "Setup timing is failing", detail)
    elif slack is not None and timing_period and slack > timing_period * 0.5:
        detail = (f"Worst setup slack is {slack:.3f} ns on a "
                  f"{timing_period} ns period, so over half of every cycle "
                  f"goes unused.")
        if prior.get("min_period"):
            detail += (f" Post-route STA measured a minimum period of "
                       f"{prior['min_period']} ns"
                       + (f" ({prior['fmax_mhz']} MHz)"
                          if prior.get("fmax_mhz") else "")
                       + f" -- about {timing_period / prior['min_period']:.1f}x "
                         f"faster than it is being asked to run. If throughput "
                         f"matters, CLK_PERIOD can come down close to that.")
        elif prior.get("fmax_mhz"):
            detail += (f" STA measured fmax = {prior['fmax_mhz']} MHz, well "
                       f"above what this period asks for.")

        # The direction of the area trade matters and is easy to state
        # backwards: a tighter period makes the mapper upsize cells and add
        # buffers, so it costs area and power rather than saving them. The
        # measured pair of mappings says whether there is any area to reclaim
        # by going the other way.
        if (timed and area and timed.get("area_um2") and area.get("area_um2")
                and abs(timed["area_um2"] - area["area_um2"]) <= 0.01):
            detail += (" Note that this margin is not costing area: mapping for "
                       "the target and mapping with no timing target both came "
                       "out at "
                       f"{timed['area_um2']:.1f} um^2, so the constraint is not "
                       "binding and relaxing it further would reclaim nothing. "
                       "Tightening it would cost area and power, not save them.")
        else:
            detail += (" Be aware of the direction of the trade: a tighter "
                       "period makes the mapper upsize cells and insert "
                       "buffers, so it buys frequency at the cost of area and "
                       "power.")
        add("medium", "Large timing headroom", detail)

    if stale_period:
        add("high", "Timing on file was measured at a different clock period",
            f"The reports in results/pnr/ were produced with a "
            f"{prior['sdc_period']} ns clock, but this analysis was asked for "
            f"{period_ns} ns. Every timing number below still describes the "
            f"{prior['sdc_period']} ns run, and the advice is computed against "
            f"that. Re-run place and route and STA at {period_ns} ns before "
            f"acting on it.")

    if prior.get("hold_slack") is not None and prior["hold_slack"] < 0:
        add("high", "Hold timing is failing",
            f"Worst hold slack is {prior['hold_slack']:.3f} ns. Hold violations are "
            f"fixed by buffer insertion during place and route, not by RTL changes; "
            f"check that the flow's hold repair step ran.")

    if levels and levels > DEEP_LOGIC_LEVELS and not violating:
        add("medium", "Deep combinational logic",
            f"The longest topological path is {levels} logic levels. Timing is met "
            f"today, but this is the path that will fail first if the clock is "
            f"tightened -- it is the natural place to pipeline.")

    # ---- structure ----
    if groups.get("muxes", 0) > 0 and generic.get("cells"):
        share = 100.0 * groups["muxes"] / generic["cells"]
        if share > 30:
            add("medium", "Multiplexer-dominated logic",
                f"{groups['muxes']} of {generic['cells']} generic cells "
                f"({share:.0f}%) are multiplexers. That usually means a wide "
                f"case/if chain or a register file addressed combinationally; "
                f"a one-hot encoding or a real memory macro is smaller and faster.")

    if groups.get("memory", 0) > 0:
        add("medium", "Inferred memory is being mapped to flip-flops",
            f"{groups['memory']} memory cell(s) survive to mapping. sky130hd has no "
            f"SRAM macro in this flow, so each bit becomes a flip-flop plus address "
            f"decode logic. For anything larger than a few words, a compiled SRAM "
            f"macro is the change that matters most for both area and timing.")

    if module.get("has_memory") and not groups.get("memory"):
        add("low", "Register array inferred as logic",
            "The RTL declares a 2-D array but it did not map to a memory cell, so "
            "it became discrete flip-flops. If it was meant to be a RAM, make the "
            "read synchronous and single-ported so the inference can fire.")

    # ---- measured area/delay trade-off ----
    if (timed and area and timed.get("area_um2") and area.get("area_um2")
            and not timed.get("error") and not area.get("error")):
        delta = timed["area_um2"] - area["area_um2"]
        pct = 100.0 * delta / timed["area_um2"] if timed["area_um2"] else 0.0
        if pct > 2.0:
            add("low", "Timing target is costing area",
                f"Mapping for the {period_ns} ns target uses "
                f"{timed['area_um2']:.1f} um^2; letting abc minimise freely uses "
                f"{area['area_um2']:.1f} um^2. The timing constraint is costing "
                f"{delta:.1f} um^2 ({pct:.1f}%). If the period has margin, relaxing "
                f"it recovers that area.")
        elif pct < -2.0:
            add("low", "Timing-driven mapping is also the smaller one",
                f"The {period_ns} ns timing-driven mapping ({timed['area_um2']:.1f} "
                f"um^2) came out smaller than the unconstrained one "
                f"({area['area_um2']:.1f} um^2). No area is being spent on timing; "
                f"the constraint is not the limiting factor.")

    # ---- physical ----
    utilisation = prior.get("utilisation")
    if utilisation is not None and utilisation > HIGH_UTILISATION_PCT:
        add("medium", "Core utilisation is high",
            f"Utilisation is {utilisation:.1f}%. Above roughly "
            f"{HIGH_UTILISATION_PCT:.0f}% the router starts fighting for tracks on "
            f"sky130hd. Lower CORE_UTIL or PLACE_DENSITY to give it room.")

    if prior.get("drc"):
        add("high", "Routing DRC violations",
            f"{int(prior['drc'])} routing DRC error(s) remain. Raising "
            f"MAX_ROUTING_LAYER or lowering PLACE_DENSITY gives detailed routing "
            f"more room to resolve them.")

    if prior.get("drv"):
        add("medium", "Electrical limit violations",
            f"{int(prior['drv'])} slew/capacitance/fanout limit violation(s). These "
            f"are fixed by resizing and buffering; a high count usually points at "
            f"one high-fanout net that needs an explicit buffer tree.")

    # ---- RTL hygiene ----
    ports = module.get("ports") or []
    if not any(p["is_reset"] for p in ports if p["direction"] == "input"):
        add("low", "No reset port",
            "No input port matched a reset name, so every flip-flop starts "
            "undefined. Simulation will show X until each register is written, and "
            "there is no way to bring the block to a known state in silicon.")

    wide = [p for p in ports if (p["width"] or 1) > 64]
    if wide:
        names = ", ".join(f"{p['name']}[{p['width']}]" for p in wide[:4])
        add("low", "Very wide ports",
            f"{names} exceed 64 bits. Every bit needs its own pad-facing pin, and "
            f"the I/O placer has to fit them on the die edge -- often the real "
            f"floorplan constraint on a small block.")

    if not tips:
        add("low", "No optimisation targets found",
            "Structure, area and timing all look reasonable for this design. Run "
            "place and route and STA first if you have not -- most of the advice "
            "here keys off measured post-route timing.")

    order = {"high": 0, "medium": 1, "low": 2}
    tips.sort(key=lambda tip: order[tip["severity"]])
    return tips


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def row(label, value, unit=""):
    if value is None:
        return f"  {label:<28} (not measured)"
    return f"  {label:<28} {value}{unit}"


def render(design, module, generic, timed, area, prior, tips, period_ns):
    title = f"Optimisation analysis: {design}"
    lines = [title, "=" * len(title), "",
             f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
             f"Target clock period {period_ns} ns", ""]

    counts = {"input": 0, "output": 0, "inout": 0}
    for port in module.get("ports") or []:
        counts[port["direction"]] = counts.get(port["direction"], 0) + 1
    lines += [
        "Interface",
        row("top module", module.get("name")),
        row("ports", f"{counts['input']} in / {counts['output']} out / "
                     f"{counts['inout']} inout"),
        row("parameters", len(module.get("params") or {})),
        "",
    ]

    groups = generic.get("groups") or {}
    lines += [
        "Structure (technology independent)",
        row("generic cells", generic.get("cells")),
        # Cells, not bits: one generic $dff cell covers a whole vector, so an
        # 8-bit register counts once here. The per-bit count comes from the
        # mapped netlist below, where every flop is a real library cell.
        row("register cells", groups.get("flipflops")),
        row("multiplexers", groups.get("muxes")),
        row("arithmetic cells", groups.get("arith")),
        row("memory cells", groups.get("memory")),
        row("logic depth", generic.get("logic_levels"), " levels"),
    ]
    if generic.get("error"):
        lines.append(f"  ! {generic['error']}")
    lines.append("")

    lines.append("Standard-cell mapping (sky130hd)")
    if timed and not timed.get("error"):
        timed_groups = timed.get("groups") or {}
        lines += [
            row(f"cells @ {period_ns} ns target", timed.get("cells")),
            row("flip-flops (per bit)", timed_groups.get("flipflops")),
            row(f"area @ {period_ns} ns target", timed.get("area_um2"), " um^2"),
        ]
    if area and not area.get("error"):
        lines += [
            row("cells, area-minimised", area.get("cells")),
            row("area, area-minimised", area.get("area_um2"), " um^2"),
        ]
    for experiment in (timed, area):
        if experiment and experiment.get("error"):
            lines.append(f"  ! {experiment['error']}")
    if not timed and not area:
        lines.append("  (skipped: PDK Liberty not available)")
    lines.append("")

    lines += [
        "Measured by earlier stages",
        row("measured at period", prior.get("sdc_period"), " ns"),
        row("post-route WNS", prior.get("wns"), " ns"),
        row("worst setup slack", prior.get("setup_slack"), " ns"),
        row("worst hold slack", prior.get("hold_slack"), " ns"),
        row("max frequency", prior.get("fmax_mhz"), " MHz"),
        row("min clock period", prior.get("min_period"), " ns"),
        row("die", prior.get("die")),
        row("utilisation", prior.get("utilisation"), "%"),
        row("total power", prior.get("power_w"), " W"),
        row("routing DRC errors", prior.get("drc")),
    ]
    if not prior.get("has_sta"):
        lines.append("  (no STA summary yet -- run place and route, then STA, for "
                     "timing-based advice)")
    lines.append("")

    lines.append("Recommendations")
    lines.append("")
    for index, tip in enumerate(tips, 1):
        lines.append(f"  {index}. [{tip['severity'].upper()}] {tip['title']}")
        for chunk in wrap(tip["detail"], 72):
            lines.append(f"       {chunk}")
        lines.append("")

    return "\n".join(lines)


def wrap(text, width):
    words, line, out = text.split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def log_to_db(db_path, design, rtl_path, tips):
    """Record the run in the designs table, best effort.

    A missing or locked database must not fail the analysis -- the report on
    disk is the deliverable.
    """
    if not db_path:
        return None
    try:
        connection = sqlite3.connect(db_path, timeout=5)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS designs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, file_path TEXT, "
            "status TEXT, suggestions TEXT, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "INSERT INTO designs (name, file_path, status, suggestions) "
            "VALUES (?,?,?,?)",
            (design, rtl_path, "optimised",
             "; ".join(f"[{t['severity']}] {t['title']}" for t in tips)),
        )
        connection.commit()
        connection.close()
        return None
    except Exception as error:  # noqa: BLE001 - reported, never fatal
        return f"database logging skipped: {error}"


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rtl")
    parser.add_argument("design")
    parser.add_argument("--period", type=float, default=10.0)
    parser.add_argument("--lib", default=None, help="PDK Liberty file")
    parser.add_argument("--pnr-dir", default=os.path.join("results", "pnr"))
    parser.add_argument("--out-dir", default=os.path.join("results", "optimize"))
    parser.add_argument("--db", default="veriopt.db")
    args = parser.parse_args()

    if not os.path.exists(args.rtl):
        sys.exit(f"optimize: RTL not found: {args.rtl}")

    parsed = parse_sources([args.rtl], top=args.design)
    module = parsed["top_module"] or {"name": args.design, "ports": [], "params": {}}

    stripped = strip_dump_block(args.rtl)
    try:
        generic = measure_generic(stripped, module["name"])

        timed = area = None
        if args.lib and os.path.exists(args.lib):
            timed = measure_mapped(stripped, module["name"], args.lib,
                                   args.period, timing_driven=True)
            area = measure_mapped(stripped, module["name"], args.lib,
                                  args.period, timing_driven=False)
    finally:
        os.unlink(stripped)

    prior = read_prior_stages(args.pnr_dir, args.design)
    tips = advise(module, generic, timed, area, prior, args.period)
    report = render(args.design, module, generic, timed, area, prior, tips,
                    args.period)

    note = log_to_db(args.db, args.design, args.rtl, tips)
    if note:
        report += f"\n{note}\n"

    os.makedirs(args.out_dir, exist_ok=True)
    text_path = os.path.join(args.out_dir, f"{args.design}_optimize.txt")
    json_path = os.path.join(args.out_dir, f"{args.design}_optimize.json")

    with open(text_path, "w") as handle:
        handle.write(report)
    with open(json_path, "w") as handle:
        json.dump({
            "design": args.design,
            "rtl": args.rtl,
            "period_ns": args.period,
            "interface": module.get("ports"),
            "generic": generic,
            "mapped_timing_driven": timed,
            "mapped_area": area,
            "prior_stages": prior,
            "recommendations": tips,
        }, handle, indent=2)

    print(report)
    print(f"Wrote {text_path}")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
