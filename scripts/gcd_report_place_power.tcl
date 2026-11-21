set project_root [file normalize [file join [file dirname [info script]] ..]]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
foreach required_env {PLACEMENT_ODB PLACEMENT_SDC ACTIVITY_VCD} {
  if {![info exists ::env($required_env)]} { error "required environment variable $required_env is not set" }
}
read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(PLACEMENT_ODB)
read_sdc $::env(PLACEMENT_SDC)
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
set upf_file [file join $project_root designs gcd flow constraints gcd.upf]
if {[info exists ::env(POWER_UPF)]} { set upf_file $::env(POWER_UPF) }
source [file join $project_root scripts load_single_domain_upf.tcl]
load_single_domain_upf $upf_file
if {[info exists ::env(ACTIVITY_TRACE)] && $::env(ACTIVITY_TRACE)} { sta::set_debug "read_vcd" 1 }
read_vcd -scope tb_gcd_activity/dut $::env(ACTIVITY_VCD)
report_activity_annotation

if {[info exists ::env(CAP_REPORT)]} {
  set cap_file [open $::env(CAP_REPORT) w]
  puts $cap_file "#DRIVER_CAP\tpin\tnet\tpin_cap\twire_cap\ttotal_cap"
  set scene [sta::cmd_scene]
  # The GCD ORFS config preserves hierarchy. Traverse every hierarchy level so
  # the exported driver loads cover the same leaf-cell outputs that OpenSTA
  # includes in report_power.
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
        set pin_name [get_full_name $pin]
        if {![dict exists $exported_drivers $pin_name]} {
          dict set exported_drivers $pin_name 1
          puts $cap_file "#DRIVER_CAP\t$pin_name\t$net_name\t$pin_cap\t$wire_cap\t$total_cap"
        }
      }
    }
    $pin_iter finish
  }
  close $cap_file
}

report_power -digits 12
report_checks -path_delay max -group_path_count 3 -digits 3
