# Export CTS sink-to-branch assignments and CTS cell input nets/capacitances.
foreach required {CL_OVPS_ODB CL_OVPS_LIBERTY CL_OVPS_SINK_OUT CL_OVPS_CELL_OUT} {
  if {![info exists ::env($required)] || $::env($required) eq ""} {
    error "required environment variable $required is not set"
  }
}
foreach liberty [split $::env(CL_OVPS_LIBERTY) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(CL_OVPS_ODB)
set block [ord::get_db_block]

proc clovps_export_clock_domain {net seen} {
  if {$net eq "NULL"} { return [list "" ""] }
  set net_name [$net getName]
  if {[dict exists $seen $net_name]} { return [list "" ""] }
  dict set seen $net_name 1
  set drivers {}
  foreach iterm [$net getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] eq "OUTPUT"} { lappend drivers $iterm }
  }
  if {[llength $drivers] > 1} { return [list "" ""] }
  if {[llength $drivers] == 1} {
    set iterm [lindex $drivers 0]
    set inst [$iterm getInst]
    set master [[$inst getMaster] getName]
    set pin [[$iterm getMTerm] getName]
    if {[regexp {__(clkbuf|clkinv|bufbuf|buf|inv)_[0-9]+$} $master]} {
      set input [$inst findITerm A]
      if {$input eq "NULL"} { return [list "" ""] }
      return [clovps_export_clock_domain [$input getNet] $seen]
    }
    if {[regexp -nocase {__(dlclkp|sdlclkp|.*clkgate|.*icg)_[0-9]+$} $master]} {
      return [list "icg:[$inst getName]" [$inst getName]]
    }
    return [list "source:[$inst getName]/$pin" ""]
  }
  foreach bterm [$net getBTerms] {
    if {[$bterm getIoType] eq "INPUT"} { return [list "port:[$bterm getName]" ""] }
  }
  return [list "" ""]
}

set sink_output [open $::env(CL_OVPS_SINK_OUT) w]
puts $sink_output "instance\tmaster\tbranch\tx_dbu\ty_dbu\tcap_ff\tclock_domain\tparent_icg_instance\tsink_class"
set cell_output [open $::env(CL_OVPS_CELL_OUT) w]
puts $cell_output "instance\tmaster\tinput_net\tinput_cap_ff\tx_dbu\ty_dbu"
foreach inst [$block getInsts] {
  set instance_name [$inst getName]
  set master [[$inst getMaster] getName]
  if {[string match "clkload*" $instance_name] || [string match "clkbuf*" $instance_name] || [regexp {__(dlclkp|sdlclkp)_[0-9]+$} $master]} {
    set input_net ""
    set input_cap_ff ""
    foreach iterm [$inst getITerms] {
      set mterm [$iterm getMTerm]
      if {[$mterm getName] eq "A" && [$mterm getIoType] eq "INPUT"} {
        set net [$iterm getNet]
        if {$net ne "NULL"} { set input_net [$net getName] }
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
    if {[$mterm getIoType] ne "INPUT"} { continue }
    set lib_pins [get_lib_pins -quiet */${master}/[$mterm getName]]
    if {[llength $lib_pins] != 1 || [get_property -quiet [lindex $lib_pins 0] is_register_clock] ne "1"} {
      continue
    }
    # Placement search is restricted to movable Sky130HD standard-cell
    # sequential sinks. Hard macros and clock-gating cells remain boundaries.
    if {![string match "sky130_fd_sc_hd__*" $master] || [regexp -nocase {(dlclkp|sdlclkp|clkgate|icg)} $master]} { continue }
    set net [$iterm getNet]
    if {$net eq "NULL"} { continue }
    lassign [clovps_export_clock_domain $net [dict create]] clock_domain parent_icg
    if {$clock_domain eq "" || [string match "net:*" $clock_domain]} { continue }
    lassign [$iterm getAvgXY] valid x y
    if {!$valid} { continue }
    set cap_ff [expr {[get_property [lindex $lib_pins 0] capacitance] * 1000.0}]
    puts $sink_output "$instance_name\t$master\t[$net getName]\t$x\t$y\t$cap_ff\t$clock_domain\t$parent_icg\tstandard_flop"
  }
}
close $sink_output
close $cell_output
exit
