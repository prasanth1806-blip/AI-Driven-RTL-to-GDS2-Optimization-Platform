import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "results"
AI_DIR = RESULTS / "ai"
PNR_DIR = RESULTS / "pnr"


def design_paths(design):
    """Return design-specific result paths for the current flow run."""
    return {
        "synth_log": PNR_DIR / f"{design}_synth.log",
        "pnr_log": PNR_DIR / f"{design}_pnr.log",
        "pnr_summary": PNR_DIR / f"{design}_summary.txt",
        "route_drc": PNR_DIR / f"{design}_route_drc.rpt",
        "sta_report": PNR_DIR / f"{design}_sta.rpt",
        "sta_summary": PNR_DIR / f"{design}_sta_summary.txt",
        "signoff_report": PNR_DIR / f"{design}_signoff.rpt",
        "signoff_summary": PNR_DIR / f"{design}_signoff_summary.txt",
    }


def current_design():
    """Find the newest RTL source, matching the Makefile/flow convention."""
    sources = sorted((ROOT / "src").glob("*.sv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return sources[0].stem if sources else "unknown"


OUTPUT = AI_DIR / "flow_data.json"


def read_text(path):
    """Read a text file safely."""
    if not path.exists():
        return ""

    try:
        return path.read_text(errors="ignore")
    except Exception:
        return ""


def read_json(path):
    """Read JSON safely."""
    if not path.exists():
        return {}

    try:
        with path.open() as f:
            return json.load(f)
    except Exception:
        return {}


def extract_number(text, patterns):
    """
    Search text using multiple regular expressions.

    Returns the first matching numeric value.
    """
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            try:
                return float(match.group(1))
            except (ValueError, IndexError):
                pass

    return None


def collect_verification():
    """
    Collect verification information from the latest Cocotb
    results.xml if available.
    """

    data = {
        "status": "UNKNOWN",
        "tests": None,
        "passed": None,
        "failed": None,
        "skipped": None,
    }

    results_xml = ROOT / "results.xml"

    text = read_text(results_xml)

    if not text:
        return data

    tests = re.search(r'tests="(\d+)"', text)
    failures = re.search(r'failures="(\d+)"', text)
    skipped = re.search(r'skipped="(\d+)"', text)

    if tests:
        data["tests"] = int(tests.group(1))

    if failures:
        data["failed"] = int(failures.group(1))

    if skipped:
        data["skipped"] = int(skipped.group(1))

    if data["tests"] is not None and data["failed"] is not None:
        data["passed"] = data["tests"] - data["failed"]

        if data["failed"] == 0:
            data["status"] = "PASS"
        else:
            data["status"] = "FAIL"

    return data


def collect_rtl_analysis(design):
    """Load the RTL analysis generated earlier."""

    path = AI_DIR / f"{design}_rtl_analysis.json"

    data = read_json(path)

    if not data:
        return {
            "available": False
        }

    return {
        "available": True,
        "data": data
    }


def collect_tb_analysis(design):
    """Load testbench analysis generated earlier."""

    path = AI_DIR / f"{design}_tb_analysis.json"

    data = read_json(path)

    if not data:
        return {
            "available": False
        }

    return {
        "available": True,
        "data": data
    }


def collect_synthesis(design):
    """Collect synthesis information for the selected design."""

    paths = design_paths(design)
    log = read_text(paths["synth_log"])
    summary = read_text(paths["pnr_summary"])

    combined = log + "\n" + summary

    data = {
        "available": bool(combined),
        "cell_count": extract_number(
            combined,
            [
                r"Number of cells:\s*([0-9]+)",
                r"cells:\s*([0-9]+)",
                r"Cell count\s*[:=]\s*([0-9]+)",
            ],
        ),
        "area": extract_number(
            combined,
            [
                r"Chip area for module.*?:\s*([0-9.]+)",
                r"area\s*[:=]\s*([0-9.]+)",
                r"Total area\s*[:=]\s*([0-9.]+)",
            ],
        ),
    }

    return data


def collect_pnr(design):
    """Collect physical design information for the selected design."""

    paths = design_paths(design)
    log = read_text(paths["pnr_log"])
    summary = read_text(paths["pnr_summary"])
    drc = read_text(paths["route_drc"])

    combined = log + "\n" + summary + "\n" + drc

    return {
        "available": bool(combined),

        "routing_drc_errors": extract_number(
            combined,
            [
                r"routing DRC errors\s*[:=]\s*([0-9]+)",
                r"DRC errors\s*[:=]\s*([0-9]+)",
                r"DRC\s*[:=]\s*([0-9]+)",
            ],
        ),

        "utilization": extract_number(
            combined,
            [
                r"utilization\s*[:=]\s*([0-9.]+)",
                r"core utilization\s*[:=]\s*([0-9.]+)",
            ],
        ),

        "area": extract_number(
            combined,
            [
                r"design area\s*[:=]\s*([0-9.]+)",
                r"area\s*[:=]\s*([0-9.]+)",
            ],
        ),

        "status": (
            "FAIL"
            if re.search(r"\bFAIL\b|\berror\b", combined, re.IGNORECASE)
            else "AVAILABLE"
        ),
    }


def collect_sta(design):
    """Collect timing information from OpenSTA."""

    paths = design_paths(design)
    report = read_text(paths["sta_report"])
    summary = read_text(paths["sta_summary"])

    combined = report + "\n" + summary

    return {
        "available": bool(combined),

        "wns": extract_number(
            combined,
            [
                r"WNS\s*[:=]\s*(-?[0-9.]+)",
                r"worst setup slack\s*[:=]\s*(-?[0-9.]+)",
            ],
        ),

        "tns": extract_number(
            combined,
            [
                r"TNS\s*[:=]\s*(-?[0-9.]+)",
                r"total negative slack\s*[:=]\s*(-?[0-9.]+)",
            ],
        ),

        "worst_hold_slack": extract_number(
            combined,
            [
                r"worst hold slack\s*[:=]\s*(-?[0-9.]+)",
            ],
        ),

        "status": (
            "PASS"
            if not re.search(
                r"\bFAIL\b",
                combined,
                re.IGNORECASE,
            )
            else "FAIL"
        ),
    }


def collect_signoff(design):
    """Collect physical signoff information."""

    paths = design_paths(design)
    report = read_text(paths["signoff_report"])
    summary = read_text(paths["signoff_summary"])

    combined = report + "\n" + summary

    return {
        "available": bool(combined),

        "status": (
            "PASS"
            if combined and not re.search(
                r"\bFAIL\b|\berror\b",
                combined,
                re.IGNORECASE,
            )
            else "UNKNOWN"
        ),

        "report_available": bool(report),
        "summary_available": bool(summary),
    }


def collect_power():
    """
    Collect power information.

    The current flow may not have a real power report.
    Therefore we do not invent a value.
    """

    possible_files = [
        PNR_DIR / "uart_tx_power.rpt",
        PNR_DIR / "uart_tx_power.txt",
        RESULTS / "power.rpt",
        RESULTS / "power.txt",
    ]

    for path in possible_files:
        text = read_text(path)

        if text:
            return {
                "available": True,
                "total_power": extract_number(
                    text,
                    [
                        r"total power\s*[:=]\s*([0-9.eE+-]+)",
                        r"Total Power\s*[:=]\s*([0-9.eE+-]+)",
                    ],
                ),
                "source": str(path.relative_to(ROOT)),
            }

    return {
        "available": False,
        "total_power": None,
        "source": None,
    }


def collect_artifacts(design):
    """Record important artifacts produced by this design's flow."""
    files = [
        "results/design.vcd",
        "results/netlist.v",
        f"results/{design}.png",
        f"results/{design}_rtl.dot",
        f"results/pnr/{design}_mapped.v",
        f"results/pnr/{design}_routed.v",
        f"results/pnr/{design}_routed.def",
        f"results/pnr/{design}.odb",
        f"results/pnr/{design}.spef",
        f"results/pnr/{design}.sdc",
        f"results/pnr/{design}_sta.rpt",
        f"results/pnr/{design}_signoff.rpt",
        f"results/pnr/{design}_route_drc.rpt",
    ]
    result = {}
    for relative_path in files:
        path = ROOT / relative_path
        result[relative_path] = {
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }
    return result


def main():
    AI_DIR.mkdir(parents=True, exist_ok=True)
    design = current_design()
    paths = design_paths(design)

    print("=" * 60)
    print("VERIOPT EDA RESULT COLLECTOR")
    print("=" * 60)
    print(f"Project : {ROOT}")
    print(f"Design  : {design}")
    print("Collecting EDA results...")
    print()

    flow_data = {
        "design": {"name": design},
        "verification": collect_verification(),
        "rtl_analysis": collect_rtl_analysis(design),
        "testbench_analysis": collect_tb_analysis(design),
        "synthesis": collect_synthesis(design),
        "physical_design": collect_pnr(design),
        "timing": collect_sta(design),
        "power": collect_power(),
        "signoff": collect_signoff(design),
        "artifacts": collect_artifacts(design),
        "evidence": {
            "synthesis_log": read_text(paths["synth_log"]),
            "pnr_summary": read_text(paths["pnr_summary"]),
            "sta_summary": read_text(paths["sta_summary"]),
            "signoff_summary": read_text(paths["signoff_summary"]),
            "optimization": read_text(RESULTS / "optimize" / f"{design}_optimize.txt"),
        },
    }

    with OUTPUT.open("w") as f:
        json.dump(
            flow_data,
            f,
            indent=2
        )

    print("Collection complete.")
    print()
    print(f"Saved:")
    print(OUTPUT)

    print()
    print("Quick summary:")
    print(
        "Verification :",
        flow_data["verification"]["status"]
    )

    print(
        "Synthesis    :",
        "AVAILABLE"
        if flow_data["synthesis"]["available"]
        else "NOT AVAILABLE"
    )

    print(
        "P&R          :",
        flow_data["physical_design"]["status"]
    )

    print(
        "STA          :",
        flow_data["timing"]["status"]
    )

    print(
        "Power        :",
        "AVAILABLE"
        if flow_data["power"]["available"]
        else "NOT AVAILABLE"
    )

    print(
        "Signoff      :",
        flow_data["signoff"]["status"]
    )


if __name__ == "__main__":
    main()