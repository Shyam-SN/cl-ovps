# Export placed register clock-pin coordinates, load, and CTS leaf branch.
#
# Required environment variables:
#   CTS_SINK_ODB   OpenROAD database checkpoint to inspect
#   CTS_SINK_OUT   Output TSV path
# Optional:
#   SKY130HD_LIB   Liberty file (defaults to this project's TT library)

foreach required {CTS_SINK_ODB CTS_SINK_OUT} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

set project_root [file normalize [file join [file dirname [info script]] ..]]
if {[info exists ::env(SKY130HD_LIB)]} {
  set liberty $::env(SKY130HD_LIB)
} else {
  set liberty [file join $project_root tools openroad-flow-scripts flow platforms \
                   sky130hd lib sky130_fd_sc_hd__tt_025C_1v80.lib]
}

read_liberty $liberty
read_db $::env(CTS_SINK_ODB)
set block [ord::get_db_block]
set output [open $::env(CTS_SINK_OUT) w]
puts $output "instance\tmaster\tclock_pin\tbranch\tx_dbu\ty_dbu\tcap_ff"

set count 0
foreach inst [$block getInsts] {
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    set pin_name [$mterm getName]
    if {[$mterm getIoType] ne "INPUT"} {
      continue
    }

    set master [[$inst getMaster] getName]
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
    # dbITerm getAvgXY returns {valid x y}; pin rather than instance locations
    # are important because TritonCTS clusters the clock-pin coordinates.
    lassign [$iterm getAvgXY] valid x y
    if {!$valid} {
      continue
    }
    set cap_ff [expr {[get_property $lib_pin capacitance] * 1000.0}]
    puts $output "[$inst getName]\t$master\t$pin_name\t[$net getName]\t$x\t$y\t$cap_ff"
    incr count
  }
}

close $output
puts "GCD_CTS_SINK_INVENTORY sinks=$count output=$::env(CTS_SINK_OUT)"
exit
