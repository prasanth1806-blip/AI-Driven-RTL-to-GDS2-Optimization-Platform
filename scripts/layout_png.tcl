# ---------------------------------------------------------------------------
# Render a PNG of a routed layout from the OpenDB database written by
# scripts/pnr.tcl.
#
# This runs as its own OpenROAD process on purpose. Rendering goes through Qt,
# and when Qt cannot bring up a platform integration it calls qFatal, which
# aborts the process outright -- there is no Tcl error to catch. Doing it here
# rather than at the end of the P&R script means a headless-rendering problem
# costs a picture, not a completed route.
#
# Environment: DESIGN_NAME, PNR_DIR. Requires QT_QPA_PLATFORM=offscreen.
# ---------------------------------------------------------------------------

set design  $::env(DESIGN_NAME)
set out_dir $::env(PNR_DIR)
set db_file $out_dir/${design}.odb
set png     $out_dir/${design}_layout.png

if {![file exists $db_file]} {
    puts "ERROR: no routed database at $db_file"
    exit 1
}

read_db $db_file

set block [ord::get_db_block]
set dbu   [[ord::get_db] getTech]
set dbu   [$dbu getDbUnitsPerMicron]

# Frame the die with a small border so the boundary isn't flush against the
# edge of the image. gui::save_image takes microns.
set die  [$block getDieArea]
set x1   [expr {[$die xMin] / double($dbu)}]
set y1   [expr {[$die yMin] / double($dbu)}]
set x2   [expr {[$die xMax] / double($dbu)}]
set y2   [expr {[$die yMax] / double($dbu)}]
set pad  [expr {0.02 * ($x2 - $x1)}]

gui::save_image $png \
    [expr {$x1 - $pad}] [expr {$y1 - $pad}] \
    [expr {$x2 + $pad}] [expr {$y2 + $pad}] \
    1600

puts "Layout image: $png"
exit 0
