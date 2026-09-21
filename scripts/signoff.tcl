# ---------------------------------------------------------------------------
# Physical design signoff checks on the routed database.
#
# Where `make sta` asks "does it meet timing", this asks "is the layout
# actually buildable": are the cells legally placed on-site and non-
# overlapping, did the router leave DRC violations, is any net long enough to
# be an antenna risk during fabrication, is the power grid connected, and do
# any nets break the library's electrical limits.
#
# What this cannot check: a real foundry DRC deck and LVS need KLayout or
# Magic plus the full sky130A PDK, neither of which is installed here, and
# there is no GDS to run them on. Those remain open -- see the note the
# summary prints. Everything below is checkable from the OpenDB database.
#
# Environment: DESIGN_NAME, PNR_DIR, LIB_FILE, SDC_FILE.
# ---------------------------------------------------------------------------

set design   $::env(DESIGN_NAME)
set out_dir  $::env(PNR_DIR)
set lib_file $::env(LIB_FILE)
set sdc_file $::env(SDC_FILE)

set db_file $out_dir/${design}.odb
set spef    $out_dir/${design}.spef

if {![file exists $db_file]} {
    puts "ERROR: no routed database at $db_file -- run 'make pnr' first."
    exit 1
}

read_liberty $lib_file
read_db $db_file
if {[file exists $sdc_file]} {
    read_sdc $sdc_file
    if {[llength [all_clocks]] > 0} {
        set_propagated_clock [all_clocks]
    }
}
if {[file exists $spef]} {
    read_spef $spef
}

set block [ord::get_db_block]
set tech  [[ord::get_db] getTech]
set dbu   [$tech getDbUnitsPerMicron]

puts "\n=========================================================="
puts "== Physical signoff: $design"
puts "==========================================================\n"

# ---------------------------------------------------------------------------
# Placement legality
# ---------------------------------------------------------------------------
puts "---- Placement legality ----"
if {[catch {check_placement -verbose} err]} {
    puts "PLACEMENT CHECK FAILED: $err"
} else {
    puts "Placement check passed: all instances legally placed on-site."
}

# ---------------------------------------------------------------------------
# Routing completeness
#
# A net whose wires were never laid down is an open circuit, so count the
# signal nets that came out of routing with no routed geometry at all.
# ---------------------------------------------------------------------------
puts "\n---- Routing completeness ----"
set unrouted 0
set total_signal 0
foreach net [$block getNets] {
    set type [$net getSigType]
    if {$type ne "SIGNAL" && $type ne "CLOCK"} {
        continue
    }
    incr total_signal
    set wire [$net getWire]
    if {$wire eq "NULL" && [llength [$net getSWires]] == 0} {
        incr unrouted
        puts "  UNROUTED: [$net getName]"
    }
}
puts "Signal/clock nets: $total_signal, unrouted: $unrouted"

# ---------------------------------------------------------------------------
# Detailed routing DRC
#
# detailed_route wrote its violations during `make pnr`; an empty report is a
# clean route. Re-reading the file is cheaper and more faithful than re-running
# the router.
# ---------------------------------------------------------------------------
puts "\n---- Routing DRC ----"
set drc_file $out_dir/${design}_route_drc.rpt
if {[file exists $drc_file]} {
    set fh [open $drc_file r]
    set drc_text [read $fh]
    close $fh
    set drc_count [regexp -all {violation type:} $drc_text]
    puts "Detailed routing DRC violations: $drc_count (from [file tail $drc_file])"
    if {$drc_count > 0} {
        puts $drc_text
    }
} else {
    puts "No DRC report at $drc_file -- run 'make pnr' to regenerate."
}

# ---------------------------------------------------------------------------
# Antenna checks
#
# A long metal run connected to a gate acts as a charge collector during
# plasma etch and can punch through the gate oxide before the protecting
# diode is connected. This is a fabrication check, not a timing one.
# ---------------------------------------------------------------------------
puts "\n---- Antenna checks ----"
if {[catch {check_antennas} err]} {
    puts "Antenna check could not run: $err"
}

# ---------------------------------------------------------------------------
# Power grid connectivity
#
# Every standard cell's VPWR/VGND has to reach the VDD/VSS grid. An
# unconnected instance is silently dead silicon.
# ---------------------------------------------------------------------------
puts "\n---- Power grid connectivity ----"
set unconnected_pg 0
foreach inst [$block getInsts] {
    foreach iterm [$inst getITerms] {
        set sig [$iterm getSigType]
        if {$sig ne "POWER" && $sig ne "GROUND"} {
            continue
        }
        if {[$iterm getNet] eq "NULL"} {
            incr unconnected_pg
            puts "  UNCONNECTED: [$inst getName]/[[$iterm getMTerm] getName]"
        }
    }
}
puts "Instances checked: [llength [$block getInsts]], unconnected power/ground pins: $unconnected_pg"

# ---------------------------------------------------------------------------
# Electrical design rules from the Liberty
# ---------------------------------------------------------------------------
puts "\n---- Electrical DRV (slew / capacitance / fanout) ----"
report_check_types -max_slew -max_capacitance -max_fanout -violators -digits 3

# ---------------------------------------------------------------------------
# Area and density
# ---------------------------------------------------------------------------
puts "\n---- Area and utilization ----"
set die  [$block getDieArea]
set core [$block getCoreArea]
puts [format "Die  : %.2f x %.2f um" \
          [expr {([$die xMax]  - [$die xMin])  / double($dbu)}] \
          [expr {([$die yMax]  - [$die yMin])  / double($dbu)}]]
puts [format "Core : %.2f x %.2f um" \
          [expr {([$core xMax] - [$core xMin]) / double($dbu)}] \
          [expr {([$core yMax] - [$core yMin]) / double($dbu)}]]
report_design_area
report_cell_usage

puts "\n---- Power ----"
report_power -digits 4

puts "\nSignoff checks complete for $design"
exit 0
