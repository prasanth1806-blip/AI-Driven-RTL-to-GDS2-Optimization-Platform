import json
import os
from pathlib import Path
from datetime import datetime
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(
    0,
    str(PROJECT_ROOT)
)

from scripts.ai.ollama_client import ask_ollama


SRC_DIR = PROJECT_ROOT / "src"
TB_DIR = PROJECT_ROOT / "tb"
RESULTS_DIR = PROJECT_ROOT / "results"

AI_DIR = RESULTS_DIR / "ai"
REPORT_DIR = RESULTS_DIR / "report"

AI_DIR.mkdir(
    parents=True,
    exist_ok=True
)

REPORT_DIR.mkdir(
    parents=True,
    exist_ok=True
)


def read_file(path):
    """
    Safely read a text file.
    """

    if not path.exists():
        return ""

    try:

        return path.read_text(
            encoding="utf-8",
            errors="ignore"
        )

    except Exception as exc:

        print(
            f"WARNING: Cannot read "
            f"{path}: {exc}"
        )

        return ""


def find_files(
    directory,
    extensions
):
    """
    Recursively find files.
    """

    if not directory.exists():
        return []

    files = []

    for extension in extensions:

        files.extend(
            directory.rglob(
                f"*{extension}"
            )
        )

    return sorted(
        set(files)
    )


def collect_rtl():

    files = find_files(
        SRC_DIR,
        [
            ".sv",
            ".v",
            ".svh",
            ".vh"
        ]
    )

    if not files:

        return "No RTL files found."

    sections = []

    for path in files:

        content = read_file(
            path
        )

        relative = path.relative_to(
            PROJECT_ROOT
        )

        sections.append(
            f"""
===== RTL FILE: {relative} =====

{content}
"""
        )

    return "\n".join(
        sections
    )


def collect_testbench():

    files = find_files(
        TB_DIR,
        [
            ".py",
            ".sv",
            ".v"
        ]
    )

    if not files:

        return "No testbench files found."

    sections = []

    for path in files:

        filename = path.name.lower()

        # Ignore Python cache files.
        if filename.startswith(
            "__"
        ):
            continue

        content = read_file(
            path
        )

        relative = path.relative_to(
            PROJECT_ROOT
        )

        sections.append(
            f"""
===== TESTBENCH FILE: {relative} =====

{content}
"""
        )

    return "\n".join(
        sections
    )


def collect_eda_results():

    if not RESULTS_DIR.exists():

        return "No EDA results found."

    extensions = [
        ".txt",
        ".log",
        ".rpt",
        ".report",
        ".json",
        ".csv"
    ]

    files = find_files(
        RESULTS_DIR,
        extensions
    )

    sections = []

    for path in files:

        relative = path.relative_to(
            PROJECT_ROOT
        )

        relative_string = str(
            relative
        ).replace(
            "\\",
            "/"
        )

        # Don't feed old AI reports back into Ollama.
        if relative_string.startswith(
            "results/ai/"
        ):
            continue

        if relative_string.startswith(
            "results/report/"
        ):
            continue

        content = read_file(
            path
        )

        if not content.strip():
            continue

        sections.append(
            f"""
===== EDA RESULT: {relative} =====

{content}
"""
        )

    if not sections:

        return (
            "No EDA result files are available."
        )

    return "\n".join(
        sections
    )


def collect_optimization_results():

    directories = [
        RESULTS_DIR / "optimize",
        RESULTS_DIR / "optimization",
        RESULTS_DIR / "opt"
    ]

    sections = []

    for directory in directories:

        if not directory.exists():
            continue

        files = find_files(
            directory,
            [
                ".txt",
                ".log",
                ".rpt",
                ".json",
                ".csv"
            ]
        )

        for path in files:

            content = read_file(
                path
            )

            if not content.strip():
                continue

            relative = path.relative_to(
                PROJECT_ROOT
            )

            sections.append(
                f"""
===== OPTIMIZATION RESULT: {relative} =====

{content}
"""
            )

    if not sections:

        return (
            "No optimization results are available."
        )

    return "\n".join(
        sections
    )


def get_design_name():

    # Makefile passes DESIGN_NAME if available.
    name = os.getenv(
        "DESIGN_NAME"
    )

    if name:
        return name

    # Otherwise detect the newest RTL file.
    rtl_files = find_files(
        SRC_DIR,
        [
            ".sv",
            ".v"
        ]
    )

    if rtl_files:

        newest = max(
            rtl_files,
            key=lambda p: p.stat().st_mtime
        )

        return newest.stem

    return "rtl_design"


def build_prompt():

    design_name = get_design_name()

    print(
        f"Preparing AI report for: "
        f"{design_name}"
    )

    rtl = collect_rtl()

    testbench = collect_testbench()

    eda_results = collect_eda_results()

    optimization = (
        collect_optimization_results()
    )

    prompt = f"""
You are an expert semiconductor EDA/CAD engineer,
RTL design engineer, verification engineer,
synthesis engineer, timing engineer,
and physical-design engineer.

You are the AI analysis engine of an
AI-Driven RTL-to-GDSII Optimization Platform.

Your job is to analyze the actual project data
provided below.

The design can be ANY digital hardware design.

Do NOT assume it is UART.

============================================================
DESIGN
============================================================

{design_name}


============================================================
RTL SOURCE
============================================================

{rtl}


============================================================
TESTBENCH / VERIFICATION SOURCE
============================================================

{testbench}


============================================================
ACTUAL EDA TOOL OUTPUTS
============================================================

{eda_results}


============================================================
OPTIMIZATION OUTPUTS
============================================================

{optimization}


============================================================
EDA/CAD EXPERTISE
============================================================

You have expertise in:

- Verilog
- SystemVerilog
- RTL design
- RTL verification
- Cocotb
- UVM
- Simulation
- Waveform analysis
- Synthesis
- Yosys
- ABC
- Standard-cell mapping
- Floorplanning
- Placement
- Clock Tree Synthesis
- CTS
- Routing
- Parasitic extraction
- OpenROAD
- OpenSTA
- Static Timing Analysis
- Setup timing
- Hold timing
- WNS
- TNS
- Power analysis
- Area optimization
- Performance optimization
- Power optimization
- DRC
- LVS
- GDSII
- Physical signoff
- RTL-to-GDSII
- EDA/CAD automation
- Python automation
- Linux EDA environments
- AI-assisted EDA optimization


============================================================
COMPLETE FLOW
============================================================

Analyze the design through the following conceptual flow:

RTL
 ↓
Verification
 ↓
Simulation
 ↓
Synthesis
 ↓
Technology Mapping
 ↓
Floorplanning
 ↓
Placement
 ↓
Clock Tree Synthesis
 ↓
Routing
 ↓
Parasitic Extraction
 ↓
STA
 ↓
Power
 ↓
DRC
 ↓
LVS
 ↓
GDSII
 ↓
Signoff


============================================================
REPORT TASKS
============================================================

1. EXECUTIVE SUMMARY

Explain the overall condition of the project.

Mention which stages have actual evidence.


2. DESIGN UNDERSTANDING

Determine what the RTL implements.

Identify:

- Top module
- Main functionality
- Inputs
- Outputs
- Clock
- Reset
- Important internal structures


3. RTL ANALYSIS

Analyze:

- Sequential logic
- Combinational logic
- FSMs
- Counters
- Arithmetic
- Muxes
- Registers
- Memories
- Control logic
- Potential RTL risks


4. VERIFICATION ANALYSIS

Analyze the actual testbench.

Discuss:

- Test scenarios
- Functional checks
- Assertions
- Corner cases
- Reset testing
- Boundary testing
- Pass/fail information


5. SIMULATION ANALYSIS

Use actual simulator output.

Report:

- PASS/FAIL
- Test count
- Failures
- Warnings
- Errors

Do not invent results.


6. SYNTHESIS ANALYSIS

Analyze actual synthesis results.

Discuss:

- Cell count
- Sequential cells
- Combinational cells
- Area
- Utilization
- Critical logic
- Synthesis warnings


7. PHYSICAL DESIGN / CAD ANALYSIS

Analyze actual:

- Floorplan
- Utilization
- Placement
- CTS
- Routing
- Congestion
- DRC
- Physical implementation


8. STATIC TIMING ANALYSIS

Analyze:

- Clock period
- Frequency
- Setup
- Hold
- WNS
- TNS
- Critical paths

Use only actual reported values.


9. POWER ANALYSIS

Analyze actual power results.

Discuss:

- Total power
- Dynamic power
- Leakage
- Clock power
- Switching

If unavailable:

"Power data not available."


10. SIGNOFF ANALYSIS

Analyze:

- DRC
- LVS
- Antenna
- Physical checks
- GDSII

Only claim PASS when evidence supports PASS.


11. PPA ANALYSIS

Analyze:

POWER
PERFORMANCE
AREA

Discuss trade-offs between them.


12. OPTIMIZATION

Give practical recommendations for:

- RTL
- Synthesis
- Timing
- Power
- Physical design

For each recommendation explain:

Problem
Recommendation
Expected direction
Trade-off
Validation method


============================================================
STRICT EVIDENCE RULES
============================================================

NEVER invent numerical results.

Do not invent:

- Area
- Power
- WNS
- TNS
- Hold slack
- Frequency
- Cell count
- Utilization
- DRC count
- LVS result
- Congestion
- Timing

If information is unavailable:

"Not available."

Do not assume:

- UART
- Simulation PASS
- DRC PASS
- LVS PASS
- GDSII generated

Separate:

ACTUAL MEASURED RESULTS

from:

AI RECOMMENDATIONS.


============================================================
FINAL REPORT FORMAT
============================================================

# AI-DRIVEN RTL-TO-GDSII OPTIMIZATION REPORT

## 1. Executive Summary

## 2. Design Understanding

## 3. RTL Analysis

## 4. Verification Analysis

## 5. Simulation Results

## 6. Synthesis Analysis

## 7. Physical Design / CAD Analysis

## 8. Static Timing Analysis

## 9. Power Analysis

## 10. DRC / LVS / Signoff Analysis

## 11. PPA Analysis

## 12. Critical Issues

## 13. RTL Optimization Recommendations

## 14. Synthesis Optimization Recommendations

## 15. Physical Design Optimization Recommendations

## 16. Overall Assessment

## 17. Recommended Next Steps

## 18. Evidence and Limitations

Be technically accurate.

Use actual tool evidence as the source of truth.
"""

    return (
        design_name,
        prompt
    )


def save_report(
    design_name,
    prompt,
    ai_report
):

    txt_file = (
        REPORT_DIR /
        f"{design_name}_ai_report.txt"
    )

    json_file = (
        AI_DIR /
        f"{design_name}_ai_report.json"
    )

    prompt_file = (
        AI_DIR /
        f"{design_name}_prompt.txt"
    )

    txt_content = f"""
AI-DRIVEN RTL-TO-GDSII
OPTIMIZATION REPORT

Design: {design_name}

Generated:
{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

============================================================

{ai_report}

============================================================
"""

    txt_file.write_text(
        txt_content,
        encoding="utf-8"
    )

    json_data = {

        "design": design_name,

        "generated_at":
            datetime.now().isoformat(),

        "model":
            os.getenv(
                "OLLAMA_MODEL",
                "gemma3:latest"
            ),

        "report":
            ai_report
    }

    json_file.write_text(
        json.dumps(
            json_data,
            indent=2
        ),
        encoding="utf-8"
    )

    # Save the exact prompt for debugging.
    prompt_file.write_text(
        prompt,
        encoding="utf-8"
    )

    print()
    print("=" * 60)
    print("AI REPORT GENERATED")
    print("=" * 60)

    print(
        f"TXT    : {txt_file}"
    )

    print(
        f"JSON   : {json_file}"
    )

    print(
        f"PROMPT : {prompt_file}"
    )

    print("=" * 60)


def main():

    print()
    print("=" * 60)
    print(
        "VERIOPT OLLAMA AI REPORT GENERATOR"
    )
    print("=" * 60)

    design_name, prompt = (
        build_prompt()
    )

    print()
    print(
        f"Prompt size: "
        f"{len(prompt)} characters"
    )

    print()
    print(
        "Sending complete project "
        "information to Ollama..."
    )

    try:

        ai_report = ask_ollama(
            prompt
        )

    except Exception as exc:

        print()
        print(
            "ERROR: AI report generation failed."
        )

        print(exc)

        return 1

    if not ai_report.strip():

        print(
            "ERROR: Ollama returned "
            "an empty response."
        )

        return 1

    save_report(
        design_name,
        prompt,
        ai_report
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )