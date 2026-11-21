# Export activity-weighted capacitance from a post-CTS global-route checkpoint.
# Required environment: STAGE_ODB, STAGE_SDC, IDLE_VCD, TYPICAL_VCD, PEAK_VCD,
# and GRT_ACTIVITY_CAP_REPORT.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {STAGE_ODB STAGE_SDC IDLE_VCD TYPICAL_VCD PEAK_VCD GRT_ACTIVITY_CAP_REPORT} {
  if {![info exists ::env($required_env)]} {
    error "required environment variable $required_env is not set"
  }
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(STAGE_ODB)
read_sdc $::env(STAGE_SDC)
source [file join $platform_dir setRC.tcl]
set_thread_count 1
estimate_parasitics -global_routing

set report [open $::env(GRT_ACTIVITY_CAP_REPORT) w]
puts $report "#GRT_DRIVER_CAP\tworkload\tclass\tpin\tnet\tpin_cap_f\twire_cap_f\ttotal_cap_f\tdensity_hz\tduty\tactivity_origin"
set scene [sta::cmd_scene]
foreach workload {idle typical peak} {
  set vcd $::env([string toupper $workload]_VCD)
  read_vcd -scope tb_gcd_activity/dut $vcd
  set exported_drivers [dict create]
  foreach net [get_nets -hierarchical *] {
    set name [get_full_name $net]
    set leaf [lindex [split $name /] end]
    set class signal
    if {$leaf eq "clk" || [string match "clknet_*" $leaf]} {
      set class clock
    }
    set pin_cap [$net pin_capacitance $scene "max"]
    set wire_cap [$net wire_capacitance $scene "max"]
    set total_cap [$net capacitance $scene "max"]
    set pin_iter [$net connected_pin_iterator]
    while {[$pin_iter has_next]} {
      set pin [$pin_iter next]
      set driver [$pin is_driver]
      if {[$pin is_top_level_port]} {
        set driver [expr {$driver && [get_property $pin pin_direction] eq "input" && [get_full_name $pin] ne "clk"}]
      }
      if {$driver && ![$pin is_pwr_gnd]} {
        set pin_name [get_full_name $pin]
        if {![dict exists $exported_drivers $pin_name]} {
          dict set exported_drivers $pin_name 1
          set activity [get_property $pin activity]
          puts $report "#GRT_DRIVER_CAP\t$workload\t$class\t$pin_name\t$name\t$pin_cap\t$wire_cap\t$total_cap\t[lindex $activity 0]\t[lindex $activity 1]\t[lindex $activity 2]"
        }
      }
    }
    $pin_iter finish
  }
}
close $report
puts "GRT_ACTIVITY_CAP exported=$::env(GRT_ACTIVITY_CAP_REPORT)"
