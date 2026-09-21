# ---------------------------------------------------------------------------
# OpenROAD place-and-route flow for veriopt.
#
# Takes the Yosys netlist already mapped to the PDK's standard cells and runs
# it through floorplan -> pin placement -> tapcells -> PDN -> global place ->
# CTS -> global route -> detailed route -> fillers, then writes out DEF, a
# routed netlist, an OpenDB database, timing/area/power reports and a PNG of
# the finished layout.
#
# Every knob comes from the environment so the same script serves any design
# and any platform; the Makefile's `pnr` target exports them.
# ---------------------------------------------------------------------------

proc env_or {name default} {
    if {[info exists ::env($name)] && $::env($name) ne ""} {
        return $::env($name)
    }
    return $default
}

proc banner {text} {
    puts "\n=========================================================="
    puts "== $text"
    puts "==========================================================\n"
}

set design       $::env(DESIGN_NAME)
set platform_dir $::env(PLATFORM_DIR)
set netlist      $::env(MAPPED_NETLIST)
set sdc_file     $::env(SDC_FILE)
set out_dir      $::env(PNR_DIR)

set tech_lef     $::env(TECH_LEF)
set sc_lef       $::env(SC_LEF)
set lib_file     $::env(LIB_FILE)

set site         [env_or PLACE_SITE unithd]
set util         [env_or CORE_UTIL 40]
set density      [env_or PLACE_DENSITY 0.60]
set core_margin  [env_or CORE_MARGIN 5.0]
set min_core     [env_or MIN_CORE_SIDE 55.0]
set tap_cell     [env_or TAP_CELL_NAME sky130_fd_sc_hd__tapvpwrvgnd_1]
set cts_buf      [env_or CTS_BUF_CELL sky130_fd_sc_hd__clkbuf_4]
set fill_cells   [env_or FILL_CELLS "sky130_fd_sc_hd__fill_1 sky130_fd_sc_hd__fill_2 sky130_fd_sc_hd__fill_4 sky130_fd_sc_hd__fill_8"]
set min_layer    [env_or MIN_ROUTING_LAYER met1]
set max_layer    [env_or MAX_ROUTING_LAYER met5]
set io_hor       [env_or IO_PLACER_H met3]
set io_ver       [env_or IO_PLACER_V met2]
set diode_cell   [env_or DIODE_CELL sky130_fd_sc_hd__diode_2]
# Repair against a tightened ratio: the repair works from global-route
# estimates, so leaving no margin lets nets that were borderline there come
# out over the limit once detailed routing lays down the real metal.
set ant_margin   [env_or ANTENNA_RATIO_MARGIN 20]

# tapcell.tcl and fastroute.tcl are sourced verbatim from the platform and
# read these out of the environment, so mirror the resolved values back.
set ::env(TAP_CELL_NAME)        $tap_cell
set ::env(MIN_ROUTING_LAYER)    $min_layer
set ::env(MAX_ROUTING_LAYER)    $max_layer
set ::env(MIN_CLK_ROUTING_LAYER) [env_or MIN_CLK_ROUTING_LAYER met3]

set_thread_count [env_or NPROC 4]

file mkdir $out_dir

# ---------------------------------------------------------------------------
# Read the technology and the mapped design
# ---------------------------------------------------------------------------
banner "Reading $design against [file tail $platform_dir]"

read_liberty $lib_file
read_lef $tech_lef
read_lef $sc_lef
read_verilog $netlist
link_design $design
read_sdc $sdc_file

# Cells the platform excludes: *probe* carry metal on every layer and the
# *lpflow* cells belong to multi-power-domain designs. Letting the resizer or
# CTS pick one produces a layout that cannot be routed or signed off.
if {[info exists ::env(DONT_USE_CELLS)] && $::env(DONT_USE_CELLS) ne ""} {
    set_dont_use $::env(DONT_USE_CELLS)
}

# ---------------------------------------------------------------------------
# Floorplan
#
# The die is sized from the design rather than fixed: sum the area of the
# mapped instances, divide by the target utilization and square it off. Small
# designs get a floor (MIN_CORE_SIDE) because the Sky130 PDN straps sit on a
# ~27 um pitch -- a core narrower than that gets no met4/met5 straps at all
# and the power grid comes out unconnected.
# ---------------------------------------------------------------------------
banner "Floorplan"

set db    [ord::get_db]
set tech  [$db getTech]
set block [ord::get_db_block]
set dbu   [$tech getDbUnitsPerMicron]

set cell_area 0.0
set inst_count 0
foreach inst [$block getInsts] {
    set master [$inst getMaster]
    set w [expr {[$master getWidth]  / double($dbu)}]
    set h [expr {[$master getHeight] / double($dbu)}]
    set cell_area [expr {$cell_area + ($w * $h)}]
    incr inst_count
}

# Sites are owned by the LEF libraries, not by the tech object, so search
# every library that was read rather than assuming which LEF declared it.
set site_obj "NULL"
foreach lib [$db getLibs] {
    set candidate [$lib findSite $site]
    if {$candidate ne "NULL"} {
        set site_obj $candidate
        break
    }
}
if {$site_obj eq "NULL"} {
    puts "ERROR: site '$site' not found in any LEF read from $platform_dir"
    exit 1
}
set site_w [expr {[$site_obj getWidth]  / double($dbu)}]
set site_h [expr {[$site_obj getHeight] / double($dbu)}]

set target_core [expr {$cell_area / ($util / 100.0)}]
set side [expr {sqrt($target_core)}]
if {$side < $min_core} {
    set side $min_core
}

# Snap the core to whole sites and the margin to whole rows, so rows tile the
# core exactly and the detailed placer has no partial site to trip over.
set core_w   [expr {ceil($side / $site_w) * $site_w}]
set core_h   [expr {ceil($side / $site_h) * $site_h}]
set margin_x [expr {ceil($core_margin / $site_w) * $site_w}]
set margin_y [expr {ceil($core_margin / $site_h) * $site_h}]
set die_w    [expr {$core_w + 2 * $margin_x}]
set die_h    [expr {$core_h + 2 * $margin_y}]

puts [format "  %d instances, %.2f um^2 of cells, target utilization %s%%" \
          $inst_count $cell_area $util]
puts [format "  die %.2f x %.2f um, core %.2f x %.2f um (margin %.2f x %.2f)" \
          $die_w $die_h $core_w $core_h $margin_x $margin_y]

initialize_floorplan \
    -die_area  [list 0 0 $die_w $die_h] \
    -core_area [list $margin_x $margin_y \
                     [expr {$margin_x + $core_w}] [expr {$margin_y + $core_h}]] \
    -site $site

source $platform_dir/make_tracks.tcl

banner "I/O pin placement"
place_pins -hor_layers $io_hor -ver_layers $io_ver

banner "Tapcells and well ties"
source $platform_dir/tapcell.tcl

banner "Power distribution network"
source $platform_dir/pdn.tcl
pdngen

# Layer RC and the signal/clock wire models, used by every downstream
# parasitic estimate.
source $platform_dir/setRC.tcl

# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------
banner "Global placement"
global_placement -density $density -pad_left 2 -pad_right 2

estimate_parasitics -placement

banner "Design repair (buffering, sizing, fanout)"
repair_design

banner "Detailed placement"
detailed_placement
optimize_mirroring
check_placement -verbose

# ---------------------------------------------------------------------------
# Clock tree synthesis -- skipped outright for combinational designs, which
# have no clock for CTS to build a tree from.
# ---------------------------------------------------------------------------
if {[llength [all_clocks]] > 0} {
    banner "Clock tree synthesis"
    repair_clock_inverters
    clock_tree_synthesis \
        -root_buf $cts_buf \
        -buf_list $cts_buf \
        -sink_clustering_enable \
        -sink_clustering_size 25 \
        -sink_clustering_max_diameter 60

    set_propagated_clock [all_clocks]
    estimate_parasitics -placement
    repair_clock_nets
    detailed_placement
} else {
    banner "Clock tree synthesis skipped (combinational design)"
}

# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
banner "Global routing"
source $platform_dir/fastroute.tcl
global_route -congestion_iterations 50 -verbose
write_guides $out_dir/${design}.guide

estimate_parasitics -global_routing

# ---------------------------------------------------------------------------
# Antenna repair
#
# A long metal run tied to a gate collects charge during plasma etch and can
# punch through the gate oxide before the protecting diode is connected;
# inserting a diode gives that charge somewhere to go.
#
# This sits between global and detailed routing on purpose. Run afterwards,
# the repair has to patch an already-detailed-routed design and leaves nets
# unrouted behind it; run here, the diodes are just more instances for the
# detailed router to wire up normally. The diodes are new cells, so the
# placement is legalized before routing sees them.
# ---------------------------------------------------------------------------
banner "Antenna repair"
if {[catch {
    repair_antennas $diode_cell -iterations 5 -ratio_margin $ant_margin
    detailed_placement
} err]} {
    puts "WARNING: antenna repair failed, continuing without diodes: $err"
}

banner "Detailed routing"
detailed_route \
    -output_drc $out_dir/${design}_route_drc.rpt \
    -verbose 0
    
# Verify against the finished detailed route, not the global estimate the
# repair worked from.
banner "Antenna verification"
check_antennas

# Fillers go in after routing: they carry no signal pins, so placing them last
# fills the leftover site gaps for well/implant continuity without getting in
# the router's way.
banner "Filler placement"
filler_placement $fill_cells
check_placement

# Re-apply the global power connections. pdngen hooked up everything that
# existed when it ran, but tapcells and fillers are placed after it -- without
# this their VPWR/VGND pins stay dangling, which the signoff connectivity
# check flags and which would be an open in the real power grid. The
# add_global_connection rules sourced from pdn.tcl are still registered on the
# block, so global_connect just re-applies them to the new instances.
global_connect

# ---------------------------------------------------------------------------
# Parasitic extraction
#
# Up to here timing has run on estimates. OpenRCX measures the real routed
# geometry against the platform's extraction rules and writes a SPEF, which is
# what `make sta` signs the block off against.
# ---------------------------------------------------------------------------
banner "Parasitic extraction (OpenRCX)"
if {[catch {
    define_process_corner -ext_model_index 0 X
    extract_parasitics -ext_model_file $platform_dir/rcx_patterns.rules
    write_spef $out_dir/${design}.spef
    puts "SPEF written: $out_dir/${design}.spef"
} err]} {
    # Extraction is a signoff refinement, not a prerequisite for a routed
    # layout. If the rules do not fit this platform, keep the route and let
    # `make sta` fall back to estimated parasitics.
    puts "WARNING: parasitic extraction failed, no SPEF written: $err"
}

# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
banner "Post-route reports"

puts "---- Design area ----"
report_design_area

puts "\n---- Cell usage ----"
report_cell_usage

puts "\n---- Timing (setup) ----"
report_checks -path_delay max -format full_clock_expanded -digits 3

puts "\n---- Timing (hold) ----"
report_checks -path_delay min -format full_clock_expanded -digits 3

puts "\n---- Slack summary ----"
report_worst_slack -max -digits 3
report_worst_slack -min -digits 3
report_tns -digits 3
report_wns -digits 3

if {[llength [all_clocks]] > 0} {
    puts "\n---- Clock skew ----"
    report_clock_skew -digits 3
}

puts "\n---- Design rule checks (slew / cap / fanout) ----"
report_check_types -max_slew -max_capacitance -max_fanout -violators

puts "\n---- Power ----"
report_power -digits 3

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
banner "Writing results"

write_def     $out_dir/${design}_routed.def
write_verilog $out_dir/${design}_routed.v
write_db      $out_dir/${design}.odb

# A second netlist without the physical-only cells. Tapcells and fillers have
# no Liberty model -- they exist purely as diffusion/well geometry -- so the
# standalone OpenSTA run in `make sta` errors out on them. The DEF and the
# routed netlist above keep them; this one is for timing tools.
write_verilog -remove_cells [concat $fill_cells $tap_cell] \
    $out_dir/${design}_sta.v

# The layout PNG is rendered afterwards by scripts/layout_png.tcl in its own
# OpenROAD process, reading the .odb just written. It is not done inline
# because the render goes through Qt, and a Qt failure is a hard abort of the
# process rather than a Tcl error -- inline, it would throw away a completed
# route that had already been written to disk. In a separate, optional
# invocation the worst case is a missing picture.

puts "\nPlace and route complete for $design -> $out_dir"
exit 0
