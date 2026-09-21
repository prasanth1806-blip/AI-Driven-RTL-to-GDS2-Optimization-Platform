#!/usr/bin/env python3

import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

RTL_DIR = PROJECT_ROOT / "src"
TB_DIR = PROJECT_ROOT / "tb"

RESULTS_DIR = PROJECT_ROOT / "results"
AI_DIR = RESULTS_DIR / "ai"

AI_DIR.mkdir(parents=True, exist_ok=True)


def find_rtl():
    """Find SystemVerilog/Verilog RTL files."""

    files = list(RTL_DIR.glob("*.sv"))
    files += list(RTL_DIR.glob("*.v"))

    return files


def find_testbench():
    """Find Cocotb Python testbenches."""

    return list(TB_DIR.glob("test_*.py"))


def read_file(path):
    """Read a text file safely."""

    return path.read_text(
        encoding="utf-8",
        errors="ignore"
    )


def analyze_rtl(rtl_text):
    """Extract basic RTL structure."""

    ports = []

    port_pattern = re.compile(
        r"\b(input|output)\s+(?:logic\s+)?"
        r"(?:\[(\d+):(\d+)\]\s+)?"
        r"(\w+)"
    )

    for match in port_pattern.finditer(rtl_text):

        direction = match.group(1)
        msb = match.group(2)
        lsb = match.group(3)
        name = match.group(4)

        if msb and lsb:
            width = abs(
                int(msb) - int(lsb)
            ) + 1
        else:
            width = 1

        ports.append(
            {
                "name": name,
                "direction": direction,
                "width": width
            }
        )

    features = {
        "reset": bool(
            re.search(
                r"\brst\b|\breset\b",
                rtl_text,
                re.IGNORECASE
            )
        ),
        "clock": bool(
            re.search(
                r"posedge\s+\w+",
                rtl_text
            )
        ),
        "busy_signal": bool(
            re.search(
                r"\bbusy\b",
                rtl_text
            )
        ),
        "counter": bool(
            re.search(
                r"\bcnt\b|\bcounter\b",
                rtl_text,
                re.IGNORECASE
            )
        ),
        "shift_register": bool(
            re.search(
                r"\bshift|\bshifter\b",
                rtl_text,
                re.IGNORECASE
            )
        )
    }

    return {
        "ports": ports,
        "features": features
    }


def analyze_testbench(tb_text):
    """Look for existing verification scenarios."""

    tests = re.findall(
        r"async\s+def\s+(test_\w+)",
        tb_text
    )

    checks = {
        "reset": bool(
            re.search(
                r"reset_dut|rst\.value",
                tb_text
            )
        ),

        "data_check": bool(
            re.search(
                r"received_data|expected|assert|AssertionError",
                tb_text
            )
        ),

        "zero_value": bool(
            re.search(
                r"0x00|\b0\b",
                tb_text
            )
        ),

        "max_value": bool(
            re.search(
                r"0xFF|255",
                tb_text,
                re.IGNORECASE
            )
        ),

        "pattern_55": bool(
            re.search(
                r"0x55|85",
                tb_text
            )
        ),

        "pattern_AA": bool(
            re.search(
                r"0xAA|170",
                tb_text,
                re.IGNORECASE
            )
        ),

        "random_testing": bool(
            re.search(
                r"random\.",
                tb_text
            )
        ),

        "busy_testing": bool(
            re.search(
                r"busy",
                tb_text
            )
        ),

        "reset_during_transmission": bool(
            re.search(
                r"reset.*transmission|transmission.*reset",
                tb_text,
                re.IGNORECASE
            )
        ),

        "back_to_back": bool(
            re.search(
                r"back.to.back|sequential",
                tb_text,
                re.IGNORECASE
            )
        )
    }

    return {
        "tests": tests,
        "checks": checks
    }


def generate_missing_cases(rtl_info, tb_info):
    """Determine useful additional scenarios."""

    missing = []

    checks = tb_info["checks"]

    if rtl_info["features"]["reset"]:
        if not checks["reset_during_transmission"]:
            missing.append(
                "Reset during transmission"
            )

    if rtl_info["features"]["busy_signal"]:
        if not checks["busy_testing"]:
            missing.append(
                "tx_start while busy"
            )

    if rtl_info["features"]["counter"]:
        missing.append(
            "Counter boundary / rollover behavior"
        )

    if rtl_info["features"]["shift_register"]:
        missing.append(
            "Shift-register boundary behavior"
        )

    if not re.search(
        r"tx_start.*1.*cycles|held.*tx_start",
        "",
        re.IGNORECASE
    ):
        missing.append(
            "tx_start held HIGH for multiple cycles"
        )

    missing.append(
        "Random reset timing"
    )

    missing.append(
        "Longer random data sequence"
    )

    return missing


def main():

    rtl_files = find_rtl()
    tb_files = find_testbench()

    if not rtl_files:
        print("ERROR: No RTL files found.")
        return 1

    if not tb_files:
        print("ERROR: No testbench files found.")
        return 1

    rtl_file = rtl_files[0]
    tb_file = tb_files[0]

    rtl_text = read_file(rtl_file)
    tb_text = read_file(tb_file)

    rtl_info = analyze_rtl(rtl_text)
    tb_info = analyze_testbench(tb_text)

    missing = generate_missing_cases(
        rtl_info,
        tb_info
    )

    report = {
        "design": rtl_file.stem,
        "rtl_file": str(
            rtl_file.relative_to(PROJECT_ROOT)
        ),
        "testbench_file": str(
            tb_file.relative_to(PROJECT_ROOT)
        ),
        "rtl": rtl_info,
        "testbench": tb_info,
        "missing_test_cases": missing
    }

    output_file = (
        AI_DIR /
        f"{rtl_file.stem}_tb_analysis.json"
    )

    output_file.write_text(
        json.dumps(
            report,
            indent=2
        ),
        encoding="utf-8"
    )

    print()
    print("=" * 60)
    print("VERIOPT TESTBENCH ANALYSIS")
    print("=" * 60)

    print(f"RTL       : {rtl_file}")
    print(f"Testbench : {tb_file}")

    print()
    print("Tests found:")

    for test in tb_info["tests"]:
        print(f"  ✓ {test}")

    print()
    print("Verification checks:")

    for name, status in tb_info["checks"].items():

        symbol = "✓" if status else "✗"

        print(
            f"  {symbol} {name}"
        )

    print()
    print("Potential additional tests:")

    for case in missing:
        print(
            f"  ⚠ {case}"
        )

    print()
    print(f"Saved: {output_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
