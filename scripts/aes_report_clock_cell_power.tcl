# Report clock-tree internal power by instance at CTS or after extraction.
# Required: AES_POWER_ODB, AES_POWER_SDC. Optional: AES_POWER_SPEF, AES_ACTIVITY_VCD.
foreach required {AES_POWER_ODB AES_POWER_SDC} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(AES_POWER_ODB)
read_sdc $::env(AES_POWER_SDC)
source [file join $platform_dir setRC.tcl]
set_thread_count 1

if {[info exists ::env(AES_POWER_SPEF)] && [file isfile $::env(AES_POWER_SPEF)]} {
  read_spef $::env(AES_POWER_SPEF)
} else {
  estimate_parasitics -placement
}
if {[info exists ::env(AES_ACTIVITY_VCD)] && [file isfile $::env(AES_ACTIVITY_VCD)]} {
  read_vcd -scope tb_aes_activity/dut $::env(AES_ACTIVITY_VCD)
}

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
set cells [concat $buffers $dummies $inverters]
puts "AES_CLOCK_CELL_POWER buffers=[llength $buffers] dummies=[llength $dummies] inverters=[llength $inverters]"
report_power -instances $cells -digits 12
if {[info exists ::env(AES_REPORT_DESIGN_POWER)] && $::env(AES_REPORT_DESIGN_POWER)} {
  report_power -digits 12
  puts "AES_CLOCK_CELL_TIMING setup_ns=[expr {[sta::worst_slack_cmd max] * 1.0e9}] hold_ns=[expr {[sta::worst_slack_cmd min] * 1.0e9}]"
}
exit
