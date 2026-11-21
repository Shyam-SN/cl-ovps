# Export placed instance dimensions and VCD-derived output activity for the
# truth-table-driven Stage 3D candidate analyzer.
foreach required {PASS3D_ODB PASS3D_SDC PASS3D_LIB PASS3D_RC_TCL PASS3D_VCD PASS3D_SCOPE PASS3D_INVENTORY} {
  if {![info exists ::env($required)] || $::env($required) eq ""} { error "$required is required" }
}
read_liberty $::env(PASS3D_LIB)
read_db $::env(PASS3D_ODB)
read_sdc $::env(PASS3D_SDC)
source $::env(PASS3D_RC_TCL)
set_thread_count 1
set_propagated_clock [all_clocks]
estimate_parasitics -placement
read_vcd -scope $::env(PASS3D_SCOPE) $::env(PASS3D_VCD)
report_activity_annotation -report_unannotated
set block [ord::get_db_block]
set dbu [$block getDbUnitsPerMicron]
set output [open $::env(PASS3D_INVENTORY) w]
puts $output "instance\tmaster\twidth_dbu\theight_dbu\tx_dbu\ty_dbu\torient\tstatus\toutput_pin\tactivity_hz\tactivity_origin\tdbu_per_micron"
foreach inst [$block getInsts] {
  set master_obj [$inst getMaster]
  set master [$master_obj getName]
  lassign [$inst getLocation] x y
  set name [$inst getName]
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] ne "OUTPUT"} { continue }
    set pin_name [$mterm getName]
    set sta_pin [get_pins -quiet "$name/$pin_name"]
    set density ""
    set origin "unknown"
    if {[llength $sta_pin] == 1 && ![catch {set activity [get_property -quiet [lindex $sta_pin 0] activity]}] && [llength $activity] >= 3} {
      if {[string is double -strict [lindex $activity 0]]} { set density [lindex $activity 0] }
      set origin [lindex $activity 2]
    }
    puts $output [join [list $name $master [$master_obj getWidth] [$master_obj getHeight] $x $y [$inst getOrient] [$inst getPlacementStatus] $pin_name $density $origin $dbu] "\t"]
  }
}
close $output
exit
