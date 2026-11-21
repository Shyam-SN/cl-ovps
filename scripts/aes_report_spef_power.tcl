set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required {AES_POWER_ODB AES_POWER_SDC AES_POWER_SPEF AES_ACTIVITY_VCD} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(AES_POWER_ODB)
read_sdc $::env(AES_POWER_SDC)
source [file join $platform_dir setRC.tcl]
read_spef $::env(AES_POWER_SPEF)
set_thread_count 1
read_vcd -scope tb_aes_activity/dut $::env(AES_ACTIVITY_VCD)

# The functional gate-level VCD intentionally omits route-inserted antenna
# diodes. Their DIODE ports connect to nets whose activity has already been
# propagated from the VCD (or from the clock definition). Copy that observed
# activity onto every physical-only pin, including a measured zero toggle rate,
# so the report distinguishes a stable net from a missing activity mapping.
set antenna_pins_annotated 0
set antenna_pins_skipped 0
foreach antenna [get_cells -quiet ANTENNA_*] {
  foreach pin [get_pins -quiet -of_objects $antenna] {
    if {[get_property -quiet $pin lib_pin_name] ne "DIODE"} { continue }
    set activity [get_property -quiet $pin activity]
    if {[llength $activity] < 2 || ![string is double -strict [lindex $activity 0]] ||
        ![string is double -strict [lindex $activity 1]]} {
      incr antenna_pins_skipped
      continue
    }
    set density_per_second [lindex $activity 0]
    set density_per_time_unit [expr {$density_per_second * [sta::time_ui_sta 1.0]}]
    set_power_activity -pins $pin -density $density_per_time_unit -duty [lindex $activity 1]
    incr antenna_pins_annotated
  }
}
puts "AES_ANTENNA_ACTIVITY annotated=$antenna_pins_annotated skipped=$antenna_pins_skipped"
report_activity_annotation -report_unannotated

set block [ord::get_db_block]
set buffers {}
set dummies {}
set inverters {}
foreach inst [$block getInsts] {
  set name [$inst getName]
  set master [[$inst getMaster] getName]
  if {[string match "clkload*" $name]} {
    lappend dummies [get_cells -quiet $name]
  } elseif {[string match "clkbuf*" $name]} {
    if {[regexp {__clkbuf_[0-9]+$} $master]} {
      lappend buffers [get_cells -quiet $name]
    } elseif {[regexp {__clkinv_[0-9]+$} $master]} {
      lappend inverters [get_cells -quiet $name]
    } else {
      error "unrecognized CTS cell master $master on $name"
    }
  }
}
set clock_cells [concat $buffers $dummies $inverters]
puts "AES_SPEF_CLOCK_CELLS buffers=[llength $buffers] dummies=[llength $dummies] inverters=[llength $inverters]"
report_power -instances $clock_cells -digits 12
puts "AES_SPEF_DESIGN_POWER"
report_power -digits 12
puts "AES_SPEF_TIMING setup_ns=[expr {[sta::worst_slack_cmd max] * 1.0e9}] hold_ns=[expr {[sta::worst_slack_cmd min] * 1.0e9}]"
exit
