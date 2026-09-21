# The place-and-route recipes pipe OpenROAD through `tee` and rely on
# `set -o pipefail` to notice a failure on the left of the pipe. /bin/sh here
# is dash, which has no pipefail, so a crashed tool would silently look like a
# clean run.
SHELL := /bin/bash

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------
SRC_DIR      = src
TB_DIR       = tb
RESULTS_DIR  = results
SCRIPTS_DIR  = scripts
SIM_BUILD_DIR = sim_build

# Interpreter used for the helper scripts. Overridable so the flow can be
# pointed at a specific virtualenv:  make wave PYTHON=/path/to/venv/bin/python
PYTHON       ?= python3

# Auto-pick the most recently generated design/testbench unless overridden,
# e.g.  make synth DESIGN=src/controller.sv
DESIGN       ?= $(shell ls -t $(SRC_DIR)/*.sv 2>/dev/null | head -n1)
DESIGN_NAME  := $(basename $(notdir $(DESIGN)))

TESTBENCH_SV  = $(TB_DIR)/test_$(DESIGN_NAME).sv
TESTBENCH_PY  = $(TB_DIR)/test_$(DESIGN_NAME).py

VCD          = $(RESULTS_DIR)/design.vcd
NETLIST      = $(RESULTS_DIR)/netlist.v
WAVE_PNG     = $(RESULTS_DIR)/design.png
SYNTH_INPUT  = $(RESULTS_DIR)/_synth_input.sv
TIMESCALE_F  = $(RESULTS_DIR)/_timescale.f
COCOTB_DUMP_V = $(RESULTS_DIR)/_cocotb_dump.v
RTL_DOT_PREFIX = $(RESULTS_DIR)/$(DESIGN_NAME)_rtl
RTL_DOT      = $(RTL_DOT_PREFIX).dot
SCHEMATIC_PNG = $(RESULTS_DIR)/$(DESIGN_NAME).png

# Testbench generation. TB_LANG picks the flavour `make tb` writes:
#   sv     -> tb/test_<design>.sv, run by `make sim`
#   python -> tb/test_<design>.py, run by `make sim-cocotb`
# TB_CYCLES is how many stimulus cycles the generated harness drives.
TB_LANG      ?= sv
TB_CYCLES    ?= 32

# Consolidated report and optimisation analysis.
REPORT_DIR   = $(RESULTS_DIR)/report
OPT_DIR      = $(RESULTS_DIR)/optimize
REPORT_TXT   = $(REPORT_DIR)/$(DESIGN_NAME)_report.txt
REPORT_HTML  = $(REPORT_DIR)/$(DESIGN_NAME)_report.html
OPT_TXT      = $(OPT_DIR)/$(DESIGN_NAME)_optimize.txt

# ---------------------------------------------------------------------------
# Physical implementation (OpenROAD + Sky130HD)
#
# OpenROAD is not packaged for Ubuntu and this machine has no root, so it
# lives in a user-local prefix installed by scripts/setup_openroad.sh, with a
# launcher on PATH. The PDK is the sky130hd platform bundle from
# OpenROAD-flow-scripts -- tech LEF, merged standard-cell LEF, Liberty, and
# the track/tapcell/PDN/RC tcl fragments that scripts/pnr.tcl sources.
# Point PLATFORM_DIR elsewhere to swap PDKs.
# ---------------------------------------------------------------------------
PLATFORM      ?= sky130hd
PLATFORM_DIR  ?= $(HOME)/Desktop/OpenROAD-flow-scripts/flow/platforms/$(PLATFORM)

TECH_LEF      ?= $(PLATFORM_DIR)/lef/sky130_fd_sc_hd.tlef
SC_LEF        ?= $(PLATFORM_DIR)/lef/sky130_fd_sc_hd_merged.lef
LIB_FILE      ?= $(PLATFORM_DIR)/lib/sky130_fd_sc_hd__tt_025C_1v80.lib

PLACE_SITE    ?= unithd
TAP_CELL_NAME ?= sky130_fd_sc_hd__tapvpwrvgnd_1
CTS_BUF_CELL  ?= sky130_fd_sc_hd__clkbuf_4
DIODE_CELL    ?= sky130_fd_sc_hd__diode_2
TIE_CELL      ?= sky130_fd_sc_hd__conb_1
ABC_DRIVER    ?= sky130_fd_sc_hd__buf_1
FILL_CELLS    ?= sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8
IO_PLACER_H   ?= met3
IO_PLACER_V   ?= met2
MIN_ROUTING_LAYER     ?= met1
MIN_CLK_ROUTING_LAYER ?= met3
MAX_ROUTING_LAYER     ?= met5

# The platform excludes these from placement: *probe* cells carry metal on
# every layer and *lpflow* cells belong to multi-power-domain designs, so
# letting the resizer or CTS choose one yields an unroutable layout.
DONT_USE_CELLS ?= sky130_fd_sc_hd__probe_p_8 sky130_fd_sc_hd__probec_p_8 \
                  sky130_fd_sc_hd__lpflow_*

# Implementation knobs -- override on the command line, e.g.
#   make pnr CLK_PERIOD=5 CORE_UTIL=55
CLK_PERIOD    ?= 10.0
CORE_UTIL     ?= 40
PLACE_DENSITY ?= 0.60
CORE_MARGIN   ?= 5.0
MIN_CORE_SIDE ?= 55.0
NPROC         ?= $(shell nproc 2>/dev/null || echo 4)

PNR_DIR        = $(RESULTS_DIR)/pnr
MAPPED_NETLIST = $(PNR_DIR)/$(DESIGN_NAME)_mapped.v
SDC_FILE       = $(PNR_DIR)/$(DESIGN_NAME).sdc
USER_SDC       = constraints/$(DESIGN_NAME).sdc
PNR_LOG        = $(PNR_DIR)/$(DESIGN_NAME)_pnr.log
PNR_SUMMARY    = $(PNR_DIR)/$(DESIGN_NAME)_summary.txt
ROUTED_DEF     = $(PNR_DIR)/$(DESIGN_NAME)_routed.def
LAYOUT_PNG     = $(PNR_DIR)/$(DESIGN_NAME)_layout.png
ROUTE_DRC      = $(PNR_DIR)/$(DESIGN_NAME)_route_drc.rpt
STA_LOG        = $(PNR_DIR)/$(DESIGN_NAME)_sta.rpt
STA_SUMMARY    = $(PNR_DIR)/$(DESIGN_NAME)_sta_summary.txt
SIGNOFF_LOG    = $(PNR_DIR)/$(DESIGN_NAME)_signoff.rpt
SIGNOFF_SUMMARY = $(PNR_DIR)/$(DESIGN_NAME)_signoff_summary.txt

# Environment shared by every OpenROAD/OpenSTA invocation below. Kept in one
# variable so the P&R, STA and signoff recipes cannot drift apart on which
# PDK or which design they are pointed at.
PDK_ENV = DESIGN_NAME="$(DESIGN_NAME)" \
          PLATFORM_DIR="$(PLATFORM_DIR)" \
          PNR_DIR="$(PNR_DIR)" \
          TECH_LEF="$(TECH_LEF)" \
          SC_LEF="$(SC_LEF)" \
          LIB_FILE="$(LIB_FILE)" \
          SDC_FILE="$(SDC_FILE)"

.PHONY: all flow tb sim sim-cocotb collect-vcd synth wave wave-gui rtl schematic report optimize clean \
        synth-pdk sdc pnr layout-png sta signoff backend pnr-gui pnr-clean setup-openroad

all: sim synth wave schematic

# ---------------------------------------------------------------------------
# Everything, in dependency order: testbench, simulation, waveform, generic
# synthesis, schematic, the full physical backend, then the consolidated
# report and the optimisation analysis.
#
# This is what the dashboard's "Generate All" button runs. It is sequential on
# purpose -- report and optimize read what the earlier stages measured, so
# running them first would produce a report full of "not run".
# ---------------------------------------------------------------------------
flow: tb sim synth schematic backend report optimize
	@echo "Full flow complete for $(DESIGN_NAME)."
	@echo "  report:   $(REPORT_TXT)"
	@echo "  optimize: $(OPT_TXT)"

# ---------------------------------------------------------------------------
# Testbench generation
#
# scripts/gen_tb.py reads the design's real port list and builds a harness
# around it, so this works on an uploaded design and not only on the built-in
# template. The generated testbench drives stimulus and dumps a VCD; it does
# not check functional correctness, because nothing here knows what the design
# is meant to compute.
#
# TB_LANG=sv writes tb/test_<design>.sv for `make sim`;
# TB_LANG=python writes tb/test_<design>.py for `make sim-cocotb`.
# ---------------------------------------------------------------------------
tb:
	@echo "Generating $(TB_LANG) testbench for $(DESIGN_NAME)..."
	@mkdir -p $(TB_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate or upload a design first."; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/gen_tb.py $(DESIGN) \
	    $(if $(filter python,$(TB_LANG)),$(TESTBENCH_PY),$(TESTBENCH_SV)) \
	    --lang $(TB_LANG) --top $(DESIGN_NAME) \
	    --cycles $(TB_CYCLES) --period $(shell echo '$(CLK_PERIOD)' | cut -d. -f1)

# ---------------------------------------------------------------------------
# Simulation (SystemVerilog testbench via Icarus Verilog)
# The design's own $dumpfile("<name>.vcd") call determines the VCD filename;
# it lands in the project root since we don't cd, matching backend.py's
# glob.glob("*.vcd") + rename-to-results/design.vcd logic.
# ---------------------------------------------------------------------------
sim:
	@echo "Running simulation for $(DESIGN_NAME)..."
	@mkdir -p $(RESULTS_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate a design first."; exit 1; \
	    elif [ ! -f "$(TESTBENCH_SV)" ]; then \
	        echo "Missing SystemVerilog testbench: $(TESTBENCH_SV)"; exit 1; \
	    elif ! command -v iverilog >/dev/null 2>&1; then \
	        echo "iverilog not found. Install with: sudo apt-get install iverilog"; exit 1; \
	    else \
	        iverilog -g2012 -o $(RESULTS_DIR)/$(DESIGN_NAME).vvp $(DESIGN) $(TESTBENCH_SV) && \
	        vvp $(RESULTS_DIR)/$(DESIGN_NAME).vvp; \
	    fi
	@$(MAKE) --no-print-directory collect-vcd

# The testbench's own $dumpfile("<name>.vcd") decides the filename, and since
# the simulation runs from the project root that is where the file lands --
# while `make wave` and the dashboard both read $(VCD). Relocating it here,
# rather than in the caller, is what lets `make sim && make wave` work from a
# plain shell; previously only the Flask backend moved the file, so the
# command-line path silently rendered a stale waveform or none at all.
collect-vcd:
	@latest=$$(ls -t *.vcd 2>/dev/null | head -n1); \
	    if [ -n "$$latest" ]; then \
	        mkdir -p $(RESULTS_DIR); \
	        mv -f "$$latest" $(VCD); \
	        echo "VCD: $(VCD)"; \
	    else \
	        latest=$$(ls -t $(SIM_BUILD_DIR)/*.vcd 2>/dev/null | head -n1); \
	        fst=$$(ls -t $(SIM_BUILD_DIR)/*.fst 2>/dev/null | head -n1); \
	        if [ -n "$$latest" ]; then \
	            mkdir -p $(RESULTS_DIR); \
	            cp -f "$$latest" $(VCD); \
	            echo "VCD: $(VCD) (from $$latest)"; \
	        elif [ -n "$$fst" ] && command -v fst2vcd >/dev/null 2>&1; then \
	            mkdir -p $(RESULTS_DIR); \
	            fst2vcd "$$fst" -o $(VCD) >/dev/null && \
	            echo "VCD: $(VCD) (converted from $$fst)"; \
	        else \
	            echo "No .vcd was produced. The testbench needs a \$$dumpfile/\$$dumpvars block;"; \
	            echo "'make tb' generates one. Waveform stages have nothing to read."; \
	            exit 1; \
	        fi; \
	    fi

# ---------------------------------------------------------------------------
# Simulation (Python/cocotb testbench)
# Requires cocotb + Icarus Verilog installed. VERILOG_SOURCES/TOPLEVEL/MODULE
# are passed straight through to cocotb's own Makefile.
#
# The -f timescale command file is not optional. Under cocotb only the RTL is
# compiled -- there is no Verilog testbench to carry a `timescale directive --
# so a design that does not declare one of its own gets Icarus's default of
# 1-second precision, and cocotb then refuses to start with "Unable to
# accurately represent 10(ns) with the simulator precision of 1e0". Forcing
# 1ns/1ps here makes the cocotb path work on RTL that has no timescale, which
# is most RTL.
#
# The dump wrapper is ours rather than cocotb's WAVES=1 one for a concrete
# reason: cocotb adds its dump module with `VERILOG_SOURCES += ...`, and a
# command-line variable assignment -- which is how VERILOG_SOURCES has to be
# passed here -- overrides that append. So WAVES=1 silently produced no
# waveform at all. Compiling our own dump module keeps the VCD, gives it the
# same name the SystemVerilog path produces, and needs `-s veriopt_dump` so
# Icarus keeps it as a root alongside the design. WAVES is left unset, not set
# to 0: WAVES=0 makes cocotb pass `-none` to vvp, which suppresses every dump
# including ours ("VCD info: dumping is suppressed").
# ---------------------------------------------------------------------------
sim-cocotb:
	@echo "Running cocotb simulation for $(DESIGN_NAME)..."
	@mkdir -p $(RESULTS_DIR) waves
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate a design first."; exit 1; \
	    elif [ ! -f "$(TESTBENCH_PY)" ]; then \
	        echo "Missing cocotb testbench: $(TESTBENCH_PY)"; exit 1; \
	    elif ! command -v cocotb-config >/dev/null 2>&1; then \
	        echo "cocotb not found. Install with: pip install cocotb"; exit 1; \
	    fi
	@echo '+timescale+1ns/1ps' > $(TIMESCALE_F)
	@printf '%s\n' \
	    '// Generated by VeriOpt.' \
	    'module veriopt_dump;' \
	    '    initial begin' \
	    '        $$dumpfile("$(DESIGN_NAME).vcd");' \
	    '        $$dumpvars(0, $(DESIGN_NAME));' \
	    '    end' \
	    'endmodule' > $(COCOTB_DUMP_V)
	@echo "Using Cocotb Python: $(shell cocotb-config --python-bin)"
	@PYTHON=$(shell cocotb-config --python-bin) \
	    PYTHONPATH=$(abspath $(TB_DIR)):$$PYTHONPATH \
	    $(MAKE) --no-print-directory \
	        -f $(shell cocotb-config --makefiles)/Makefile.sim \
	        SIM=icarus \
	        TOPLEVEL_LANG=verilog \
	        VERILOG_SOURCES="$(abspath $(DESIGN)) $(abspath $(COCOTB_DUMP_V))" \
	        TOPLEVEL=$(DESIGN_NAME) \
	        MODULE=test_$(DESIGN_NAME) \
	        COMPILE_ARGS="-g2012 -s veriopt_dump -f $(abspath $(TIMESCALE_F))"
	@$(MAKE) --no-print-directory collect-vcd
# ---------------------------------------------------------------------------
synth:
	@echo "Running Yosys synthesis for $(DESIGN_NAME)..."
	@mkdir -p $(RESULTS_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate a design first."; exit 1; \
	    fi
	@if ! command -v verilator >/dev/null 2>&1; then \
	        echo "verilator not found. Install with: sudo apt-get install verilator"; exit 1; \
	    fi
	@if ! command -v yosys >/dev/null 2>&1; then \
	        echo "yosys not found. Install with: sudo apt-get install yosys"; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/strip_dumpblock.py $(DESIGN) $(SYNTH_INPUT)
	@verilator --lint-only $(SYNTH_INPUT) || (echo "Lint failed: RTL is malformed. Regenerate or hand-fix $(DESIGN) before synthesizing." && exit 1)
	@yosys -p "read_verilog -sv $(SYNTH_INPUT); synth; write_verilog $(NETLIST)"

# ---------------------------------------------------------------------------
# Waveform PNG (rendered from the VCD produced by `make sim`)
#
# Drawn by scripts/vcd_png.py, not by gtkwave. gtkwave has no command-line
# image export: its -o is --optimize (recode VCD to FST) and -T takes a Tcl
# init *file*, so the old `gtkwave -T -o $(WAVE_PNG) $(VCD)` consumed the PNG
# path as -T's argument, treated the .vcd as a save file, and never wrote an
# image. The caller logged the failure as best-effort, which is why it went
# unnoticed.
#
# Rendering it in matplotlib also removes the environment fragility that made
# gtkwave hard to run headless: no X display, no GTK module loading, and none
# of the Snap path leakage described under `wave-gui` below.
#
# WAVE_SIGNALS caps how many signals are drawn; WAVE_SCOPE narrows to one
# scope, e.g.  make wave WAVE_SCOPE=dut
# ---------------------------------------------------------------------------
WAVE_SIGNALS ?= 24
WAVE_SCOPE   ?=

wave:
	@echo "Rendering waveform PNG for $(DESIGN_NAME)..."
	@if [ ! -f "$(VCD)" ]; then \
	        echo "VCD file not found at $(VCD). Run 'make sim' first."; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/vcd_png.py $(VCD) $(WAVE_PNG) \
	    --max-signals $(WAVE_SIGNALS) \
	    $(if $(WAVE_SCOPE),--scope $(WAVE_SCOPE),)

# ---------------------------------------------------------------------------
# Open the waveform in the gtkwave GUI (needs a display; local use only).
#
# gtkwave is cleared of a set of GTK/XDG env vars before running. When this
# Makefile (or the Flask backend that calls it) is launched from VS Code's
# integrated terminal, VS Code's own Snap packaging leaks GTK_PATH,
# GTK_EXE_PREFIX, XDG_DATA_DIRS, etc. into the shell environment. gtkwave
# then tries to load GTK modules/theme engines from VS Code's Snap directory
# instead of the system ones, which crashes with a symbol lookup error
# against a mismatched libpthread. Explicitly unsetting these before the
# gtkwave call avoids that regardless of which terminal invoked `make`.
# ---------------------------------------------------------------------------
GTKWAVE_ENV_CLEAN = env -u GTK_EXE_PREFIX -u GTK_PATH -u GIO_MODULE_DIR \
                        -u GTK_IM_MODULE_FILE -u GDK_PIXBUF_MODULE_FILE \
                        -u XDG_DATA_DIRS -u XDG_DATA_HOME \
                        -u GSETTINGS_SCHEMA_DIR -u LOCPATH

wave-gui:
	@if [ ! -f "$(VCD)" ]; then \
	        echo "VCD file not found at $(VCD). Run 'make sim' first."; exit 1; \
	    elif ! command -v gtkwave >/dev/null 2>&1; then \
	        echo "gtkwave not found. Install with: sudo apt-get install gtkwave"; exit 1; \
	    else \
	        $(GTKWAVE_ENV_CLEAN) gtkwave $(VCD); \
	    fi

# ---------------------------------------------------------------------------
# RTL schematic (Graphviz .dot, via Yosys `show`)
# Uses the same dump-block-stripped input as synthesis, so the schematic
# reflects synthesizable structure only, not simulation-only boilerplate.
#
# `show` is given an explicit module selection. Without one it renders every
# module in the design, and a hierarchical design then yields a .dot file
# holding several digraphs -- which `dot` cannot be handed safely, see the
# stdin note on the schematic target below. Showing the top is also what is
# wanted here: submodules appear as boxes, which is what an RTL schematic is.
# ---------------------------------------------------------------------------
rtl:
	@echo "Generating RTL schematic for $(DESIGN_NAME)..."
	@mkdir -p $(RESULTS_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate a design first."; exit 1; \
	    fi
	@if ! command -v yosys >/dev/null 2>&1; then \
	        echo "yosys not found. Install with: sudo apt-get install yosys"; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/strip_dumpblock.py $(DESIGN) $(SYNTH_INPUT)
	@yosys -p "read_verilog -sv $(SYNTH_INPUT); \
	           hierarchy -top $(DESIGN_NAME); \
	           proc; opt; \
	           show -format dot -prefix $(RTL_DOT_PREFIX) $(DESIGN_NAME)"

# ---------------------------------------------------------------------------
# RTL schematic PNG (renders the .dot produced by `make rtl` via Graphviz)
# ---------------------------------------------------------------------------
# `dot` gets its stdin closed and a time limit. Graphviz reads stdin for
# further graphs once it reaches the end of a named input file, so with an
# inherited stdin -- which is what a subprocess of the web server gets -- it
# blocks forever after writing a perfectly good PNG. The timeout covers the
# other failure mode: layout on a large graph is superlinear and can run for
# far longer than anyone is waiting.
DOT_TIMEOUT ?= 120

schematic: rtl
	@echo "Generating RTL schematic PNG for $(DESIGN_NAME)..."
	@if ! command -v dot >/dev/null 2>&1; then \
	        echo "graphviz 'dot' not found. Install with: sudo apt-get install graphviz"; exit 1; \
	    fi
	@timeout $(DOT_TIMEOUT) dot -Tpng $(RTL_DOT) -o $(SCHEMATIC_PNG) </dev/null || { \
	        echo "graphviz 'dot' failed or exceeded $(DOT_TIMEOUT)s on $(RTL_DOT)."; \
	        echo "The .dot file is still there if you want to render it by hand."; \
	        exit 1; \
	    }
	@echo "Schematic written to $(SCHEMATIC_PNG)"

# ---------------------------------------------------------------------------
# Install OpenROAD + the Sky130HD platform into a user-local prefix.
# ---------------------------------------------------------------------------
setup-openroad:
	@bash $(SCRIPTS_DIR)/setup_openroad.sh

# ---------------------------------------------------------------------------
# Technology mapping
#
# `make synth` produces a generic netlist, which is fine for inspection but
# has no physical cells for OpenROAD to place. This target re-synthesizes
# against the PDK Liberty so every gate in the netlist is a real sky130 cell
# with a known footprint, timing and pin geometry.
#
# The extra passes past `synth` matter downstream:
#   dfflibmap  maps generic flops to sky130 flops (abc only handles combinational logic)
#   abc -D     targets the clock period, in ps, so mapping is timing-driven
#   hilomap    replaces literal 1'b1/1'b0 with real tie cells; a bare constant
#              has nothing to place and the router has no pin to connect to
#   setundef   drives don't-cares to a definite value rather than leaving x
#   splitnets  breaks vectors into scalars, matching what the LEF pins expose
# ---------------------------------------------------------------------------
synth-pdk:
	@echo "Mapping $(DESIGN_NAME) to $(PLATFORM) standard cells..."
	@mkdir -p $(PNR_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate a design first."; exit 1; \
	    fi
	@if ! command -v yosys >/dev/null 2>&1; then \
	        echo "yosys not found. Install with: sudo apt-get install yosys"; exit 1; \
	    fi
	@if [ ! -f "$(LIB_FILE)" ]; then \
	        echo "PDK Liberty not found at $(LIB_FILE)."; \
	        echo "Run 'make setup-openroad' to fetch the $(PLATFORM) platform."; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/strip_dumpblock.py $(DESIGN) $(SYNTH_INPUT)
	@yosys -p "read_verilog -sv $(SYNTH_INPUT); \
	           hierarchy -check -top $(DESIGN_NAME); \
	           synth -top $(DESIGN_NAME) -flatten; \
	           dfflibmap -liberty $(LIB_FILE); \
	           abc -liberty $(LIB_FILE) -D $(shell echo '$(CLK_PERIOD) * 1000' | bc -l | cut -d. -f1); \
	           hilomap -hicell $(TIE_CELL) HI -locell $(TIE_CELL) LO; \
	           setundef -zero; \
	           splitnets; \
	           opt_clean -purge; \
	           stat -liberty $(LIB_FILE); \
	           write_verilog -noattr $(MAPPED_NETLIST)" 2>&1 | tee $(PNR_DIR)/$(DESIGN_NAME)_synth.log
	@echo "Mapped netlist: $(MAPPED_NETLIST)"

# ---------------------------------------------------------------------------
# Timing constraints
#
# A hand-written constraints/<design>.sdc always wins. Otherwise one is
# derived from the RTL port list, because a design with no create_clock gives
# CTS nothing to build and produces empty timing reports.
# ---------------------------------------------------------------------------
sdc:
	@mkdir -p $(PNR_DIR)
	@if [ -f "$(USER_SDC)" ]; then \
	        echo "Using hand-written constraints: $(USER_SDC)"; \
	        cp $(USER_SDC) $(SDC_FILE); \
	    else \
	        $(PYTHON) $(SCRIPTS_DIR)/gen_sdc.py $(DESIGN) $(DESIGN_NAME) $(CLK_PERIOD) $(SDC_FILE); \
	    fi

# ---------------------------------------------------------------------------
# Place and route
#
# Runs scripts/pnr.tcl under OpenROAD: floorplan, pin placement, tapcells,
# PDN, global + detailed placement, CTS, global + detailed routing, fillers,
# then reports and a layout PNG.
#
# QT_QPA_PLATFORM=offscreen: the .deb is a GUI build, so the layout render
# goes through Qt. There is no display when this runs from the Flask backend,
# and without the offscreen platform plugin Qt aborts the whole process.
# ---------------------------------------------------------------------------
pnr: synth-pdk sdc
	@echo "Running OpenROAD place and route for $(DESIGN_NAME) on $(PLATFORM)..."
	@if [ -x "$$HOME/Desktop/OpenROAD/build/bin/openroad" ]; then \
		OPENROAD="$$HOME/Desktop/OpenROAD/build/bin/openroad"; \
	elif command -v openroad >/dev/null 2>&1; then \
		OPENROAD="$$(command -v openroad)"; \
	else \
		echo "OpenROAD not found."; \
		exit 1; \
	fi; \
	echo "Using OpenROAD: $$OPENROAD"; \
	if [ ! -f "$(TECH_LEF)" ] || [ ! -f "$(SC_LEF)" ]; then \
		echo "PDK LEF files missing under $(PLATFORM_DIR)."; \
		echo "Run 'make setup-openroad' to fetch the $(PLATFORM) platform."; \
		exit 1; \
	fi; \
	set -o pipefail; \
	DESIGN_NAME="$(DESIGN_NAME)" \
	PLATFORM="$(PLATFORM)" \
	PLATFORM_DIR="$(PLATFORM_DIR)" \
	MAPPED_NETLIST="$(MAPPED_NETLIST)" \
	SDC_FILE="$(SDC_FILE)" \
	PNR_DIR="$(PNR_DIR)" \
	TECH_LEF="$(TECH_LEF)" \
	SC_LEF="$(SC_LEF)" \
	LIB_FILE="$(LIB_FILE)" \
	PLACE_SITE="$(PLACE_SITE)" \
	CORE_UTIL="$(CORE_UTIL)" \
	PLACE_DENSITY="$(PLACE_DENSITY)" \
	CORE_MARGIN="$(CORE_MARGIN)" \
	MIN_CORE_SIDE="$(MIN_CORE_SIDE)" \
	TAP_CELL_NAME="$(TAP_CELL_NAME)" \
	CTS_BUF_CELL="$(CTS_BUF_CELL)" \
	DIODE_CELL="$(DIODE_CELL)" \
	FILL_CELLS="$(FILL_CELLS)" \
	DONT_USE_CELLS="$(DONT_USE_CELLS)" \
	IO_PLACER_H="$(IO_PLACER_H)" \
	IO_PLACER_V="$(IO_PLACER_V)" \
	MIN_ROUTING_LAYER="$(MIN_ROUTING_LAYER)" \
	MIN_CLK_ROUTING_LAYER="$(MIN_CLK_ROUTING_LAYER)" \
	MAX_ROUTING_LAYER="$(MAX_ROUTING_LAYER)" \
	NPROC="$(NPROC)" \
	QT_QPA_PLATFORM=offscreen \
	"$$OPENROAD" -no_init -exit "$(SCRIPTS_DIR)/pnr.tcl" 2>&1 | tee "$(PNR_LOG)"
	@$(MAKE) --no-print-directory layout-png DESIGN=$(DESIGN) || \
		echo "WARNING: layout PNG not rendered; DEF and reports are still valid."
	@$(PYTHON) $(SCRIPTS_DIR)/summarize.py pnr $(PNR_LOG) $(PNR_SUMMARY) $(ROUTE_DRC)
	@echo "Routed DEF:  $(ROUTED_DEF)"
	@echo "Layout PNG:  $(LAYOUT_PNG)"
# ---------------------------------------------------------------------------
# Render the routed layout to PNG from the OpenDB database.
#
# Separate from `pnr` because the render goes through Qt, and a Qt failure is
# an abort rather than a catchable error -- keeping it out of the P&R process
# means a headless-rendering problem cannot discard a finished route. `pnr`
# invokes this and tolerates failure; run it by hand to retry a render.
# ---------------------------------------------------------------------------
layout-png:
	@if [ ! -f "$(PNR_DIR)/$(DESIGN_NAME).odb" ]; then \
	        echo "No routed database at $(PNR_DIR)/$(DESIGN_NAME).odb. Run 'make pnr' first."; exit 1; \
	    fi
		@if [ -x "$$HOME/Desktop/OpenROAD/build/bin/openroad" ]; then \
		OPENROAD="$$HOME/Desktop/OpenROAD/build/bin/openroad"; \
	elif command -v openroad >/dev/null 2>&1; then \
		OPENROAD="$$(command -v openroad)"; \
	else \
		echo "OpenROAD not found."; \
		exit 1; \
	fi; \
	DESIGN_NAME="$(DESIGN_NAME)" \
	PNR_DIR="$(PNR_DIR)" \
	QT_QPA_PLATFORM=offscreen \
	"$$OPENROAD" -no_init -exit $(SCRIPTS_DIR)/layout_png.tcl
# ---------------------------------------------------------------------------
# Open the routed database in the OpenROAD GUI (needs a display; local use
# only, same idea as `make wave` launching gtkwave).
# ---------------------------------------------------------------------------
pnr-gui:
	@if [ ! -f "$(PNR_DIR)/$(DESIGN_NAME).odb" ]; then \
	        echo "No routed database at $(PNR_DIR)/$(DESIGN_NAME).odb. Run 'make pnr' first."; exit 1; \
	    fi
	@openroad -gui -no_init -exit_on_error \
	    -threads $(NPROC) \
	    -command "read_db $(PNR_DIR)/$(DESIGN_NAME).odb" || \
	    openroad -gui -no_init

# ---------------------------------------------------------------------------
# Post-route static timing analysis (signoff timing)
#
# Runs under the standalone OpenSTA binary rather than inside OpenROAD, on the
# routed netlist plus the SPEF that OpenRCX extracted from the real routed
# geometry. That makes it an independent check: the delays come from measured
# parasitics, not from the estimates the placer and router optimize against.
# ---------------------------------------------------------------------------
sta:
	@echo "Running post-route STA for $(DESIGN_NAME)..."
	@if ! command -v sta >/dev/null 2>&1; then \
	        echo "OpenSTA 'sta' not found on PATH."; exit 1; \
	    fi
	@if [ ! -f "$(PNR_DIR)/$(DESIGN_NAME)_sta.v" ]; then \
	        echo "No routed netlist at $(PNR_DIR)/$(DESIGN_NAME)_sta.v. Run 'make pnr' first."; exit 1; \
	    fi
	@set -o pipefail; \
	    $(PDK_ENV) sta -no_init -exit $(SCRIPTS_DIR)/sta.tcl 2>&1 | tee $(STA_LOG)
	@$(PYTHON) $(SCRIPTS_DIR)/summarize.py sta $(STA_LOG) $(STA_SUMMARY)
	@echo "STA report: $(STA_LOG)"

# ---------------------------------------------------------------------------
# Physical design signoff
#
# Asks whether the layout is buildable rather than whether it is fast:
# placement legality, unrouted nets, routing DRC, antenna risk, power-grid
# connectivity and the Liberty's electrical limits, read back off the routed
# OpenDB database.
# ---------------------------------------------------------------------------
signoff:
	@echo "Running physical signoff checks for $(DESIGN_NAME)..."
	@if ! command -v openroad >/dev/null 2>&1; then \
	        echo "openroad not found. Run 'make setup-openroad' first."; exit 1; \
	    fi
	@if [ ! -f "$(PNR_DIR)/$(DESIGN_NAME).odb" ]; then \
	        echo "No routed database at $(PNR_DIR)/$(DESIGN_NAME).odb. Run 'make pnr' first."; exit 1; \
	    fi
	@set -o pipefail; \
	    $(PDK_ENV) openroad -no_init -exit $(SCRIPTS_DIR)/signoff.tcl 2>&1 | tee $(SIGNOFF_LOG)
	@$(PYTHON) $(SCRIPTS_DIR)/summarize.py signoff $(SIGNOFF_LOG) $(SIGNOFF_SUMMARY) $(ROUTE_DRC)
	@echo "Signoff report: $(SIGNOFF_LOG)"

# Full physical implementation: map, constrain, place and route, then sign off
# on timing and on the layout.
backend: pnr sta signoff

# ---------------------------------------------------------------------------
# AI engineering report
#
# Collects the actual outputs first, then asks the local Ollama model to
# explain them. The model is never the source of measured EDA numbers.
# ---------------------------------------------------------------------------
ai-report:
	@echo "Generating AI engineering analysis for $(DESIGN_NAME)..."
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate or upload a design first."; exit 1; \
	fi
	@$(PYTHON) $(SCRIPTS_DIR)/ai/result_collector.py
	@$(PYTHON) $(SCRIPTS_DIR)/ai/generate_ai_report.py $(DESIGN_NAME) --period $(CLK_PERIOD)
	@echo "AI report: $(REPORT_DIR)/$(DESIGN_NAME)_ai_report.txt"

# ---------------------------------------------------------------------------
# Consolidated flow report
#
# Gathers the interface, which stages ran, every artefact on disk and each
# stage summary into one text file and one HTML file. Reads only what the
# other stages already wrote, so it is safe to run at any point -- stages that
# have not run are reported as not run rather than silently omitted.
# ---------------------------------------------------------------------------
report:
	@echo "Generating flow report for $(DESIGN_NAME)..."
	@mkdir -p $(REPORT_DIR) $(RESULTS_DIR)/ai
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate or upload a design first."; \
	        exit 1; \
	    fi

	@$(PYTHON) $(SCRIPTS_DIR)/gen_report.py $(DESIGN_NAME) \
	    --src $(SRC_DIR) \
	    --tb $(TB_DIR) \
	    --results $(RESULTS_DIR) \
	    --pnr-dir $(PNR_DIR) \
	    --out-dir $(REPORT_DIR) \
	    --period $(CLK_PERIOD) >/dev/null

	@echo ""
	@echo "Generating AI engineering analysis with Ollama..."

	@DESIGN_NAME="$(DESIGN_NAME)" \
	    $(PYTHON) $(SCRIPTS_DIR)/ai/generate_ai_report.py

	@echo ""
	@echo "=========================================="
	@echo "Reports generated"
	@echo "=========================================="
	@echo "Flow report:"
	@echo "  $(REPORT_TXT)"
	@echo "  $(REPORT_HTML)"
	@echo ""
	@echo "AI report:"
	@echo "  $(REPORT_DIR)/$(DESIGN_NAME)_ai_report.txt"
	@echo "  $(RESULTS_DIR)/ai/$(DESIGN_NAME)_ai_report.json"
	@echo "=========================================="

# ---------------------------------------------------------------------------
# Optimisation analysis
#
# Measures the design rather than guessing at it: Yosys `stat`/`ltp` for cell
# mix and logic depth, two standard-cell mappings (timing-driven and
# area-minimised) for the real area cost of the timing target, plus whatever
# the STA and signoff stages already measured. Advice that would need a number
# we do not have is omitted, so run this after `backend` for timing-based
# recommendations.
# ---------------------------------------------------------------------------
optimize:
	@echo "Analysing $(DESIGN_NAME) for optimisation opportunities..."
	@mkdir -p $(OPT_DIR)
	@if [ -z "$(DESIGN)" ]; then \
	        echo "No RTL design found in $(SRC_DIR). Generate or upload a design first."; exit 1; \
	    fi
	@$(PYTHON) $(SCRIPTS_DIR)/optimize.py $(DESIGN) $(DESIGN_NAME) \
	    --period $(CLK_PERIOD) \
	    $(if $(wildcard $(LIB_FILE)),--lib $(LIB_FILE),) \
	    --pnr-dir $(PNR_DIR) --out-dir $(OPT_DIR) >/dev/null
	@echo "Optimisation analysis: $(OPT_TXT)"

pnr-clean:
	@echo "Cleaning place-and-route results..."
	@rm -rf $(PNR_DIR)

clean:
	@echo "=== Cleaning VeriOpt generated files ==="

	# Python cache
	@find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

	# Generated simulation/build files
	@rm -rf sim_build
	@rm -rf results
	@rm -rf waves
	@rm -f *.vcd
	@rm -f results.xml
	@rm -f *.log
	@rm -f *.jou
	@rm -f *.xml

	# Generated testbenches
	@rm -f tb/test_*.sv
	@rm -f tb/test_*.py

	@echo "=== Clean complete ==="