# Export clock-sink branch membership and CTS cell choices from a CTS ODB.
#
# Required environment variables:
#   CTS_ORACLE_ODB
#   CTS_ORACLE_SINK_OUT
#   CTS_ORACLE_CELL_OUT

foreach required {CTS_ORACLE_ODB CTS_ORACLE_SINK_OUT CTS_ORACLE_CELL_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
set liberty [file join $project_root tools openroad-flow-scripts flow platforms \
                 sky130hd lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_liberty $liberty
read_db $::env(CTS_ORACLE_ODB)
set block [ord::get_db_block]

set sink_output [open $::env(CTS_ORACLE_SINK_OUT) w]
puts $sink_output "instance\tmaster\tbranch\tx_dbu\ty_dbu\tcap_ff"
set cell_output [open $::env(CTS_ORACLE_CELL_OUT) w]
puts $cell_output "instance\tmaster\tinput_net\tinput_cap_ff\tx_dbu\ty_dbu"

foreach inst [$block getInsts] {
  set instance_name [$inst getName]
  set master [[$inst getMaster] getName]
  set is_dummy [string match "clkload*" $instance_name]
  set is_clock_buffer [string match "clkbuf*" $instance_name]
  if {$is_dummy || $is_clock_buffer} {
    set input_net ""
    set input_cap_ff ""
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      if {[$mterm getName] eq "A" && [$mterm getIoType] eq "INPUT"} {
        set net [$iterm getNet]
        if {$net ne "NULL"} {
          set input_net [$net getName]
        }
        set lib_pins [get_lib_pins -quiet */${master}/A]
        if {[llength $lib_pins] == 1} {
          set input_cap_ff [expr {[get_property [lindex $lib_pins 0] capacitance] * 1000.0}]
        }
      }
    }
    lassign [$inst getLocation] x y
    puts $cell_output "$instance_name\t$master\t$input_net\t$input_cap_ff\t$x\t$y"
  }

  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    set pin_name [$mterm getName]
    if {[$mterm getIoType] ne "INPUT"} {
      continue
    }
    set lib_pins [get_lib_pins -quiet */${master}/${pin_name}]
    if {[llength $lib_pins] != 1} {
      continue
    }
    set lib_pin [lindex $lib_pins 0]
    if {[get_property $lib_pin is_register_clock] ne "1"} {
      continue
    }
    set net [$iterm getNet]
    if {$net eq "NULL"} {
      continue
    }
    lassign [$iterm getAvgXY] valid x y
    if {!$valid} {
      continue
    }
    set cap_ff [expr {[get_property $lib_pin capacitance] * 1000.0}]
    puts $sink_output "$instance_name\t$master\t[$net getName]\t$x\t$y\t$cap_ff"
  }
}

close $sink_output
close $cell_output
puts "GCD_CTS_ORACLE_DATA sinks_out=$::env(CTS_ORACLE_SINK_OUT) cells_out=$::env(CTS_ORACLE_CELL_OUT)"
exit
