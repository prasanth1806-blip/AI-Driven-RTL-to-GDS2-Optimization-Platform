import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(
    0,
    str(PROJECT_ROOT)
)

from scripts.ai.ollama_client import ask_ollama


SRC_DIR = PROJECT_ROOT / "src"

AI_RESULTS_DIR = (
    PROJECT_ROOT /
    "results" /
    "ai"
)

AI_RESULTS_DIR.mkdir(
    parents=True,
    exist_ok=True
)


def find_rtl_files():
    """
    Find Verilog/SystemVerilog source files.
    """

    files = []

    for extension in (
        "*.sv",
        "*.v",
        "*.svh",
        "*.vh"
    ):

        files.extend(
            SRC_DIR.rglob(extension)
        )

    return sorted(files)


def read_rtl_file(path):
    """
    Read RTL source code.
    """

    return path.read_text(
        encoding="utf-8",
        errors="ignore"
    )


def extract_modules(rtl):
    """
    Extract module names.
    """

    return re.findall(
        r"\bmodule\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)",
        rtl
    )


def extract_ports(rtl):
    """
    Extract basic input/output ports.
    """

    ports = []

    pattern = re.compile(
        r"\b(input|output|inout)\b"
        r"(?:\s+(?:wire|reg|logic|signed))?"
        r"(?:\s*\[[^\]]+\])?"
        r"\s+"
        r"([A-Za-z_][A-Za-z0-9_]*)"
    )

    for match in pattern.finditer(rtl):

        ports.append(
            {
                "direction": match.group(1),
                "name": match.group(2)
            }
        )

    return ports


def extract_rtl_features(rtl):
    """
    Extract generic RTL structural information.

    No UART-specific assumptions are made.
    """

    return {

        "always_blocks": len(
            re.findall(
                r"\balways(?:_ff|_comb|_latch)?\b",
                rtl
            )
        ),

        "always_ff_blocks": len(
            re.findall(
                r"\balways_ff\b",
                rtl
            )
        ),

        "always_comb_blocks": len(
            re.findall(
                r"\balways_comb\b",
                rtl
            )
        ),

        "if_statements": len(
            re.findall(
                r"\bif\s*\(",
                rtl
            )
        ),

        "case_statements": len(
            re.findall(
                r"\bcase\s*\(",
                rtl
            )
        ),

        "for_loops": len(
            re.findall(
                r"\bfor\s*\(",
                rtl
            )
        ),

        "while_loops": len(
            re.findall(
                r"\bwhile\s*\(",
                rtl
            )
        ),

        "assign_statements": len(
            re.findall(
                r"\bassign\b",
                rtl
            )
        ),

        "reset_keywords": len(
            re.findall(
                r"\brst\b"
                r"|\breset\b"
                r"|\bres_n\b",
                rtl,
                re.IGNORECASE
            )
        ),

        "clock_keywords": len(
            re.findall(
                r"\bclk\b"
                r"|\bclock\b",
                rtl,
                re.IGNORECASE
            )
        ),

        "always_posedge": len(
            re.findall(
                r"\balways.*?posedge\b",
                rtl,
                re.IGNORECASE
            )
        ),

        "always_negedge": len(
            re.findall(
                r"\balways.*?negedge\b",
                rtl,
                re.IGNORECASE
            )
        ),

        "nonblocking_assignments": len(
            re.findall(
                r"<= ",
                rtl
            )
        ),

        "blocking_assignments": len(
            re.findall(
                r"(?<![<>=!])=(?!=)",
                rtl
            )
        )
    }


def build_ai_prompt(
    design_name,
    rtl,
    structure
):
    """
    Build the EDA/CAD expert prompt.

    This prompt is generic and works for:
    UART, FIFO, ALU, CPU, counter,
    processor, memory controller, etc.
    """

    prompt = f"""
You are an expert semiconductor EDA/CAD engineer,
RTL design engineer, verification engineer,
and digital physical-design engineer.

You have strong knowledge of:

- Digital logic design
- Verilog
- SystemVerilog
- RTL design
- RTL coding practices
- RTL verification
- Cocotb
- UVM concepts
- Simulation
- Waveform analysis
- Logic synthesis
- Yosys
- ABC
- Standard-cell based design
- Technology mapping
- Floorplanning
- Power planning
- Placement
- Clock Tree Synthesis
- CTS
- Routing
- Parasitic extraction
- Static Timing Analysis
- OpenSTA
- OpenROAD
- Setup timing
- Hold timing
- WNS
- TNS
- Area optimization
- Power optimization
- Performance optimization
- DRC
- LVS
- GDSII
- Physical-design signoff
- RTL-to-GDSII flows
- EDA/CAD automation
- Python-based EDA automation
- Linux-based EDA flows
- AI-assisted EDA optimization

You are the AI analysis engine of an
AI-Driven RTL-to-GDSII Optimization Platform.

The supplied design can be ANY digital hardware design.

It may be:

- UART
- FIFO
- ALU
- Counter
- FSM
- CPU
- Processor
- Memory controller
- Bus interface
- DSP block
- or another RTL design.

DO NOT assume that the design is UART.

Determine the design from the supplied RTL.

============================================================
DESIGN
============================================================

Design name:

{design_name}


============================================================
RTL STRUCTURAL INFORMATION
============================================================

{json.dumps(structure, indent=2)}


============================================================
RTL SOURCE CODE
============================================================

{rtl}


============================================================
RTL-TO-GDSII FLOW KNOWLEDGE
============================================================

Analyze the RTL considering the complete digital
implementation flow:

RTL
 ↓
RTL Verification
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
Static Timing Analysis
 ↓
Power Analysis
 ↓
DRC
 ↓
LVS
 ↓
GDSII
 ↓
Physical Signoff

Your RTL analysis should consider how RTL decisions
can affect later synthesis and physical implementation.


============================================================
RTL ANALYSIS TASK
============================================================

1. DESIGN UNDERSTANDING

Identify:

- What the design appears to implement
- Main module
- Submodules
- Inputs
- Outputs
- Clock
- Reset
- Registers
- Combinational logic
- Sequential logic
- FSM/state logic
- Counters
- Arithmetic logic
- Muxes
- Memories
- Important control logic


2. RTL QUALITY

Analyze:

- Coding style
- Sequential logic
- Combinational logic
- Reset structure
- Clock structure
- Potential inferred hardware
- Potential latch risks
- Multiple-driver risks
- Width-related risks
- Signed/unsigned issues
- Blocking/nonblocking usage
- Large combinational logic
- Deep logic structures


3. VERIFICATION RISKS

Identify functionality that a testbench
should verify.

Consider:

- Reset behavior
- Boundary conditions
- State transitions
- Counter rollover
- Invalid inputs
- Back-to-back operations
- Corner cases
- Timing-sensitive behavior
- Overflow/underflow
- Protocol behavior where applicable


4. SYNTHESIS CONSIDERATIONS

Explain what hardware structures are likely
to be inferred by synthesis.

Identify possible:

- Large muxes
- Comparators
- Adders
- Subtractors
- Counters
- Registers
- Shift registers
- Multipliers
- Memories
- Priority logic
- Deep combinational paths


5. TIMING CONSIDERATIONS

Identify RTL structures that could potentially
create long critical paths.

Discuss:

- Logic depth
- Large combinational blocks
- Wide arithmetic
- Large mux structures
- Priority chains
- Complex conditions
- Fanout concerns

Do NOT invent actual timing numbers.


6. AREA CONSIDERATIONS

Identify RTL structures that may increase:

- Cell count
- Register count
- Combinational area
- Multiplexer area
- Buffering requirements

Do NOT invent area numbers.


7. POWER CONSIDERATIONS

Identify possible sources of:

- Unnecessary switching
- High toggle activity
- Large combinational activity
- Clock activity
- Unused logic

Do NOT invent power numbers.


8. PHYSICAL DESIGN CONSIDERATIONS

Explain whether the RTL structure could potentially
affect:

- Placement
- Routing
- Congestion
- Fanout
- Timing closure
- Area
- Power

Do not claim an actual physical-design problem
unless physical-design results are provided.


9. OPTIMIZATION OPPORTUNITIES

Suggest practical areas for investigation.

For every recommendation provide:

Problem
Possible solution
Expected direction of improvement
Possible trade-off
How to validate the change


============================================================
IMPORTANT EVIDENCE RULES
============================================================

- Do NOT invent simulation results.
- Do NOT invent timing numbers.
- Do NOT invent area numbers.
- Do NOT invent power numbers.
- Do NOT invent synthesis results.
- Do NOT invent P&R results.
- Do NOT invent DRC/LVS results.
- Do NOT claim GDSII was generated.
- Clearly separate observed RTL facts from recommendations.
- If information is unavailable, say "Not available".
- Do not modify the RTL.
- Do not assume the design type.


============================================================
FINAL REPORT FORMAT
============================================================

# AI RTL DESIGN ANALYSIS

## 1. Design Summary

## 2. RTL Architecture

## 3. Interface Analysis

## 4. Sequential Logic Analysis

## 5. Combinational Logic Analysis

## 6. Verification Risks

## 7. Synthesis Considerations

## 8. Timing Considerations

## 9. Area Considerations

## 10. Power Considerations

## 11. Physical Design Considerations

## 12. RTL Optimization Opportunities

## 13. Recommended Next Steps

## 14. Evidence and Limitations

Keep the report technically accurate
and understandable to an RTL/EDA engineer.
"""

    return prompt


def analyze_design(rtl_file):

    rtl = read_rtl_file(
        rtl_file
    )

    modules = extract_modules(
        rtl
    )

    ports = extract_ports(
        rtl
    )

    features = extract_rtl_features(
        rtl
    )

    design_name = rtl_file.stem

    structure = {

        "source_file": str(
            rtl_file.relative_to(
                PROJECT_ROOT
            )
        ),

        "design_name": design_name,

        "modules": modules,

        "ports": ports,

        "features": features
    }

    print()
    print("=" * 60)
    print(
        "RTL ANALYSIS: "
        + design_name
    )
    print("=" * 60)

    print(
        json.dumps(
            structure,
            indent=2
        )
    )

    print()
    print("Sending RTL to Ollama...")

    prompt = build_ai_prompt(
        design_name,
        rtl,
        structure
    )

    ai_report = ask_ollama(
        prompt
    )

    result = {

        "design": design_name,

        "source_file": str(
            rtl_file.relative_to(
                PROJECT_ROOT
            )
        ),

        "structure": structure,

        "ai_analysis": ai_report
    }

    output_file = (
        AI_RESULTS_DIR
        / f"{design_name}_rtl_analysis.json"
    )

    output_file.write_text(
        json.dumps(
            result,
            indent=2
        ),
        encoding="utf-8"
    )

    print()
    print("=" * 60)
    print("AI RTL ANALYSIS")
    print("=" * 60)

    print(ai_report)

    print()
    print("Saved:")
    print(output_file)

    return result


def main():

    rtl_files = find_rtl_files()

    if not rtl_files:

        print(
            "ERROR: No RTL files found in src/"
        )

        sys.exit(1)

    print("RTL files found:")

    for index, path in enumerate(
        rtl_files,
        start=1
    ):

        print(
            f"{index}. "
            f"{path.relative_to(PROJECT_ROOT)}"
        )

    # Current VeriOpt behavior:
    # analyze the newest/first RTL file.
    analyze_design(
        rtl_files[0]
    )


if __name__ == "__main__":
    main()