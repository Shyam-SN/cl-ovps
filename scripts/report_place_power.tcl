# Activity-based power/timing analysis at global placement.
set project_root [file normalize [file join [file dirname [info script]] ..]]
set result_dir [file join $project_root build orfs results sky130hd picorv32_baseline baseline]
set platform_dir [file join $project_root tools openroad-flow-scripts flow platforms sky130hd]
if {[info exists ::env(PLACEMENT_ODB)]} {
  set placement_odb $::env(PLACEMENT_ODB)
} else {
  set placement_odb [file join $result_dir 3_3_place_gp.odb]
}
if {[info exists ::env(PLACEMENT_SDC)]} {
  set placement_sdc $::env(PLACEMENT_SDC)
} else {
  set placement_sdc [file join $result_dir 2_floorplan.sdc]
}

read_liberty [file join $platform_dir lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $placement_odb
read_sdc $placement_sdc
source [file join $platform_dir setRC.tcl]
estimate_parasitics -placement
read_upf -file [file join $project_root designs picorv32 flow constraints picorv32.upf]

if {[info exists ::env(ACTIVITY_TRACE)] && $::env(ACTIVITY_TRACE)} {
  # OpenSTA prints the VCD-derived transition density for each mapped pin.
  # The scorer consumes these lines to build a per-driver-pin cost table.
  sta::set_debug "read_vcd" 1
}
read_vcd -scope tb_picorv32_activity/dut $::env(ACTIVITY_VCD)
report_activity_annotation

if {[info exists ::env(CAP_REPORT)]} {
  # Export the same placement-estimated load capacitance used by STA's
  # switching-power calculation. Values are in OpenSTA's SI capacitance unit.
  set cap_file [open $::env(CAP_REPORT) w]
  puts $cap_file "#DRIVER_CAP\tpin\tnet\tpin_cap\twire_cap\ttotal_cap"
  set scene [sta::cmd_scene]
  foreach net [get_nets *] {
    set net_name [get_full_name $net]
    set pin_cap [$net pin_capacitance $scene "max"]
    set wire_cap [$net wire_capacitance $scene "max"]
    set total_cap [$net capacitance $scene "max"]
    set pin_iter [$net connected_pin_iterator]
    while {[$pin_iter has_next]} {
      set pin [$pin_iter next]
      if {[$pin is_driver] && ![$pin is_top_level_port] && ![$pin is_pwr_gnd]} {
        puts $cap_file "#DRIVER_CAP\t[get_full_name $pin]\t$net_name\t$pin_cap\t$wire_cap\t$total_cap"
      }
    }
    $pin_iter finish
  }
  close $cap_file
}

report_power -digits 6
report_checks -path_delay max -group_path_count 3 -digits 3
