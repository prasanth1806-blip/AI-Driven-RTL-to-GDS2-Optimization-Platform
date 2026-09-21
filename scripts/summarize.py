#!/usr/bin/env python3
"""Condense an OpenROAD/OpenSTA log into a short, dashboard-sized summary.

Each backend stage produces thousands of lines of tool chatter. What anyone
actually needs is the handful of numbers that say whether the stage passed:
die size and utilization after P&R, worst slack after STA, violation counts
after signoff. This pulls exactly those out of the log the Makefile tees, and
prints a one-line verdict on top.

Usage: summarize.py <stage> <log> <out.txt> [drc_report]
       stage = pnr | sta | signoff
"""
import os
import re
import sys


NO_REG_PATHS = "n/a (no register-to-register paths)"


def grab(text, pattern, flags=0):
    """Return the LAST match of `pattern`, or None.

    Last, not first: several numbers are reported more than once as a stage
    progresses (area before and after fillers, slack before and after
    routing), and the final one describes the finished result.
    """
    matches = re.findall(pattern, text, flags)
    return matches[-1].strip() if matches else None


def slack_pair(log, banner="---- Slack summary ----"):
    """Return (setup, hold) worst slack.

    report_worst_slack prints a bare "worst slack <value>" with no hint of
    which corner produced it, so the two calls are told apart by position: the
    scripts run -max (setup) then -min (hold) under this banner, with nothing
    else printed between them.
    """
    section = log.rpartition(banner)[2]
    values = re.findall(r"worst slack(?:\s+(?:max|min))?\s+(-?[\d.]+)", section)
    setup = values[0] if len(values) > 0 else None
    hold = values[1] if len(values) > 1 else None
    return setup, hold


def count_drc(drc_path):
    """Violation count from a detailed-route DRC report, or None if absent.

    TritonRoute writes each violation as a `violation type:` line; an empty
    file means a clean route.
    """
    if not drc_path or not os.path.exists(drc_path):
        return None
    with open(drc_path, errors="replace") as handle:
        return len(re.findall(r"^\s*violation type:", handle.read(), re.M))


def row(label, value, unit=""):
    if value is None:
        return f"  {label:<24} (not reported)"
    return f"  {label:<24} {value}{unit}"


def summarize_pnr(log, drc_path):
    setup, hold = slack_pair(log)
    wns = grab(log, r"^wns(?:\s+max)?\s+(-?[\d.]+)", re.M)
    tns = grab(log, r"^tns(?:\s+max)?\s+(-?[\d.]+)", re.M)
    drc = count_drc(drc_path)

    lines = [
        "Floorplan",
        row("die", grab(log, r"die ([\d.]+ x [\d.]+ um)")),
        row("core", grab(log, r"core ([\d.]+ x [\d.]+ um)")),
        row("std cells placed", grab(log, r"(\d+) instances, [\d.]+ um\^2")),
        row("design area", grab(log, r"Design area ([\d.]+ u\^2)")),
        row("utilization", grab(log, r"Design area [\d.]+ u\^2 ([\d.]+%) utilization")),
        "",
        "Timing (post-route estimate)",
        row("worst setup slack", setup, " ns"),
        row("worst hold slack", hold, " ns"),
        row("WNS", wns, " ns"),
        row("TNS", tns, " ns"),
        "",
        "Route",
        row("routing DRC errors", drc),
        row("SPEF extracted", "yes" if "SPEF written" in log else "no"),
        row("total power", grab(log, r"^Total\s+\S+\s+\S+\s+\S+\s+(\S+)\s+100\.0%", re.M), " W"),
    ]

    verdict = []
    slack = wns if wns is not None else setup
    if slack is not None:
        verdict.append("timing met" if float(slack) >= 0 else "SETUP VIOLATION")
    if drc == 0:
        verdict.append("DRC clean")
    elif drc:
        verdict.append(f"{drc} DRC violations")
    return lines, verdict


def summarize_sta(log, _drc_path):
    setup, hold = slack_pair(log)
    wns = grab(log, r"^wns(?:\s+max)?\s+(-?[\d.]+)", re.M)
    tns = grab(log, r"^tns(?:\s+max)?\s+(-?[\d.]+)", re.M)
    annotated = "Annotated parasitics from" in log

    # report_check_types prints one "VIOLATED" per failing limit.
    drv = len(re.findall(r"VIOLATED", log))

    # A design whose only paths run port -> flop -> port has no launch/capture
    # pair, so skew is undefined and fmax comes back as "inf". Reporting that
    # as "0.00 ns" would read as a catastrophic result rather than an absent
    # measurement, so name what actually happened.
    if "No launch/capture paths found" in log:
        skew = period_min = fmax = NO_REG_PATHS
    else:
        skew = grab(log, r"(-?[\d.]+)\s+setup skew")
        period_min = grab(log, r"period_min\s*=\s*([\d.]+)")
        fmax = grab(log, r"fmax\s*=\s*([\d.]+|inf)")

    lines = [
        "Timing (signoff, post-route)",
        row("parasitics", "extracted SPEF" if annotated else "ideal nets (no SPEF)"),
        row("worst setup slack", setup, " ns"),
        row("worst hold slack", hold, " ns"),
        row("WNS", wns, " ns"),
        row("TNS", tns, " ns"),
        row("clock skew", skew, " ns" if skew and skew != NO_REG_PATHS else ""),
        row("min clock period", period_min, " ns" if period_min and period_min != NO_REG_PATHS else ""),
        row("max clock frequency", fmax, " MHz" if fmax and fmax != NO_REG_PATHS else ""),
        "",
        "Checks",
        row("slew/cap/fanout violations", drv),
        row("total power", grab(log, r"^Total\s+\S+\s+\S+\s+\S+\s+(\S+)\s+100\.0%", re.M), " W"),
    ]

    verdict = []
    slack = wns if wns is not None else setup
    if slack is not None:
        verdict.append("setup met" if float(slack) >= 0 else "SETUP VIOLATION")
    if hold is not None:
        verdict.append("hold met" if float(hold) >= 0 else "HOLD VIOLATION")
    if not annotated:
        verdict.append("no SPEF: optimistic")
    return lines, verdict


def summarize_signoff(log, drc_path):
    placement_ok = "Placement check passed" in log
    unrouted = grab(log, r"unrouted:\s+(\d+)")
    pg_unconnected = grab(log, r"unconnected power/ground pins:\s+(\d+)")
    drc = count_drc(drc_path)

    # check_antennas ends with "[INFO ANT-0002] Found N net violations."
    # Absent that line the checker did not run, which is not the same as zero.
    antenna = grab(log, r"Found (\d+) net violations")

    drv = len(re.findall(r"VIOLATED", log))

    lines = [
        "Physical checks",
        row("placement legality", "PASS" if placement_ok else "FAIL"),
        row("unrouted nets", unrouted),
        row("routing DRC errors", drc),
        row("antenna violations", antenna),
        row("unconnected PG pins", pg_unconnected),
        row("slew/cap/fanout violations", drv),
        "",
        "Area",
        row("die", grab(log, r"^Die\s+:\s+([\d.]+ x [\d.]+ um)", re.M)),
        row("core", grab(log, r"^Core\s+:\s+([\d.]+ x [\d.]+ um)", re.M)),
        row("design area", grab(log, r"Design area ([\d.]+ u\^2)")),
        row("utilization", grab(log, r"Design area [\d.]+ u\^2 ([\d.]+%) utilization")),
        row("total power", grab(log, r"^Total\s+\S+\s+\S+\s+\S+\s+(\S+)\s+100\.0%", re.M), " W"),
        "",
        "Not covered here",
        "  Foundry DRC deck and LVS need KLayout or Magic plus the full",
        "  sky130A PDK and a GDS stream, none of which is installed. The",
        "  checks above are everything verifiable from the OpenDB database.",
    ]

    failures = []
    if not placement_ok:
        failures.append("illegal placement")
    if unrouted and int(unrouted) > 0:
        failures.append(f"{unrouted} unrouted nets")
    if drc:
        failures.append(f"{drc} DRC violations")
    if antenna and antenna != "0":
        failures.append(f"{antenna} antenna violations")
    if pg_unconnected and int(pg_unconnected) > 0:
        failures.append(f"{pg_unconnected} unconnected PG pins")
    verdict = failures if failures else ["all physical checks clean"]
    return lines, verdict


STAGES = {
    "pnr": ("OpenROAD place-and-route summary", summarize_pnr),
    "sta": ("Post-route static timing analysis summary", summarize_sta),
    "signoff": ("Physical design signoff summary", summarize_signoff),
}


def main():
    if len(sys.argv) < 4 or sys.argv[1] not in STAGES:
        sys.exit("usage: summarize.py <pnr|sta|signoff> <log> <out.txt> [drc_report]")

    stage, log_path, out_path = sys.argv[1:4]
    drc_path = sys.argv[4] if len(sys.argv) > 4 else None

    with open(log_path, errors="replace") as handle:
        log = handle.read()

    title, builder = STAGES[stage]
    body, verdict = builder(log, drc_path)

    header = [title, "=" * len(title), ""]
    if verdict:
        header += [f"Result: {', '.join(verdict)}", ""]

    text = "\n".join(header + body) + "\n"
    with open(out_path, "w") as handle:
        handle.write(text)
    print(text)


if __name__ == "__main__":
    main()
