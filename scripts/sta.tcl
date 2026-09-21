# ---------------------------------------------------------------------------
# Post-route static timing analysis, run under standalone OpenSTA.
#
# This is the signoff timing run, deliberately separate from the timing
# reported during place and route: it reads the routed netlist back from disk
# and annotates the SPEF that OpenRCX extracted from the real routed geometry,
# so the delays come from measured parasitics rather than from the estimates
# the placer and router work with.
#
# Environment: DESIGN_NAME, PNR_DIR, LIB_FILE, SDC_FILE.
# ---------------------------------------------------------------------------

set design   $::env(DESIGN_NAME)
set out_dir  $::env(PNR_DIR)
set lib_file $::env(LIB_FILE)
set sdc_file $::env(SDC_FILE)

set netlist $out_dir/${design}_sta.v
set spef    $out_dir/${design}.spef

foreach f [list $lib_file $netlist $sdc_file] {
    if {![file exists $f]} {
        puts "ERROR: missing input $f -- run 'make pnr' first."
        exit 1
    }
}

read_liberty $lib_file
read_verilog $netlist
link_design $design

# Without the SPEF, OpenSTA falls back to whatever wire load model the Liberty
# carries -- which for sky130 is none, so nets look ideal and the numbers are
# optimistic. Say so loudly rather than quietly reporting a better result.
if {[file exists $spef]} {
    read_spef $spef
    puts "Annotated parasitics from [file tail $spef]"
    set annotated 1
} else {
    puts "WARNING: $spef not found -- timing computed with ideal (zero-RC) nets."
    set annotated 0
}

read_sdc $sdc_file

# After CTS the clock tree is real silicon, so its insertion delay and skew
# must be walked rather than idealized.
if {[llength [all_clocks]] > 0} {
    set_propagated_clock [all_clocks]
}

set_units -time ns -capacitance pF -resistance kOhm -voltage V -current mA

puts "\n=========================================================="
puts "== Post-route STA: $design"
puts "==   library    [file tail $lib_file]"
puts "==   netlist    [file tail $netlist]"
puts "==   parasitics [expr {$annotated ? [file tail $spef] : {none (estimated)}}]"
puts "==========================================================\n"

if {$annotated} {
    puts "---- Parasitic annotation coverage ----"
    report_parasitic_annotation
}

if {[llength [all_clocks]] > 0} {
    puts "\n---- Clocks ----"
    report_clock_properties
}

puts "\n---- Setup: 5 worst paths ----"
report_checks -path_delay max -group_count 5 -format full_clock_expanded -digits 3

puts "\n---- Hold: 5 worst paths ----"
report_checks -path_delay min -group_count 5 -format full_clock_expanded -digits 3

puts "\n---- Unconstrained paths ----"
report_checks -unconstrained -digits 3

puts "\n---- Slack summary ----"
report_worst_slack -max -digits 3
report_worst_slack -min -digits 3
report_tns -digits 3
report_wns -digits 3

if {[llength [all_clocks]] > 0} {
    puts "\n---- Clock skew ----"
    report_clock_skew -digits 3

    puts "\n---- Minimum clock period achievable ----"
    report_clock_min_period
}

puts "\n---- Design rule violations (slew / capacitance / fanout) ----"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

puts "\n---- Pulse width checks ----"
report_check_types -min_pulse_width -digits 3

puts "\n---- Power ----"
report_power -digits 4

puts "\nSTA complete for $design"
exit 0
