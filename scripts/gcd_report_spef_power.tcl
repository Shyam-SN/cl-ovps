set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {SPEF_ODB SPEF_SDC SPEF_FILE ACTIVITY_VCD WORKLOAD} {
  if {![info exists ::env($required_env)]} { error "required environment variable $required_env is not set" }
}
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(SPEF_ODB)
read_sdc $::env(SPEF_SDC)
read_spef $::env(SPEF_FILE)
set upf_file [file join $project_root designs gcd flow constraints gcd.upf]
if {[info exists ::env(POWER_UPF)]} { set upf_file $::env(POWER_UPF) }
source [file join $project_root scripts load_single_domain_upf.tcl]
load_single_domain_upf $upf_file
set_thread_count 1
if {[info exists ::env(ACTIVITY_TRACE)] && $::env(ACTIVITY_TRACE)} { sta::set_debug "read_vcd" 1 }
read_vcd -scope tb_gcd_activity/dut $::env(ACTIVITY_VCD)
report_activity_annotation

if {[info exists ::env(CAP_REPORT)]} {
  set cap_file [open $::env(CAP_REPORT) w]
  puts $cap_file "#SPEF_DRIVER_CAP\tpin\tnet\tpin_cap\twire_cap\ttotal_cap\tdensity_hz\tduty\tactivity_origin"
  set scene [sta::cmd_scene]
  # The GCD ORFS config preserves hierarchy. Traverse all levels so this
  # routed capacitance export covers the same leaf outputs as OpenSTA power.
  # Hierarchical aliases can expose one leaf driver on more than one net
  # object; power is charged once per driver, so emit each pin only once.
  set exported_drivers [dict create]
  foreach net [get_nets -hierarchical *] {
    set net_name [get_full_name $net]
    set pin_cap [$net pin_capacitance $scene "max"]
    set wire_cap [$net wire_capacitance $scene "max"]
    set total_cap [$net capacitance $scene "max"]
    set pin_iter [$net connected_pin_iterator]
    while {[$pin_iter has_next]} {
      set pin [$pin_iter next]
      set export_driver [$pin is_driver]
      if {[$pin is_top_level_port]} {
        set export_driver [expr {$export_driver && [get_property $pin pin_direction] eq "input" && [get_full_name $pin] ne "clk"}]
      }
      if {$export_driver && ![$pin is_pwr_gnd]} {
        set activity [get_property $pin activity]
        set pin_name [get_full_name $pin]
        if {![dict exists $exported_drivers $pin_name]} {
          dict set exported_drivers $pin_name 1
          puts $cap_file "#SPEF_DRIVER_CAP\t$pin_name\t$net_name\t$pin_cap\t$wire_cap\t$total_cap\t[lindex $activity 0]\t[lindex $activity 1]\t[lindex $activity 2]"
        }
      }
    }
    $pin_iter finish
  }
  close $cap_file
}

set scene [sta::cmd_scene]
set switching_w [lindex [sta::design_power $scene] 1]
set setup_ns [expr {[sta::worst_slack_cmd max] * 1.0e9}]
set hold_ns [expr {[sta::worst_slack_cmd min] * 1.0e9}]
puts [format "SPEF_METRICS workload=%s switching_w=%.12e setup_ns=%.9f hold_ns=%.9f" $::env(WORKLOAD) $switching_w $setup_ns $hold_ns]
report_activity_annotation
report_power -digits 12
report_checks -path_delay max -group_path_count 1 -digits 6
report_checks -path_delay min -group_path_count 1 -digits 6
