# Export clock-buffer levels and sequential sink depths for parity-safe swaps.
foreach required {CLOCK_LEVEL_ODB CLOCK_LEVEL_BUFFERS_OUT CLOCK_LEVEL_SINKS_OUT} {
  if {![info exists ::env($required)]} { error "required environment variable $required is not set" }
}
set project_root [file normalize [file join [file dirname [info script]] ..]]
read_liberty [file join $project_root tools openroad-flow-scripts flow platforms sky130hd lib sky130_fd_sc_hd__tt_025C_1v80.lib]
read_db $::env(CLOCK_LEVEL_ODB)
set block [ord::get_db_block]

proc clock_upstream {net seen} {
  if {$net eq "NULL"} { return [list 0 0] }
  set net_name [$net getName]
  if {[dict exists $seen $net_name]} { error "clock tree cycle at $net_name" }
  dict set seen $net_name 1
  foreach iterm [$net getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] ne "OUTPUT"} { continue }
    set inst [$iterm getInst]
    set master [[$inst getMaster] getName]
    if {[regexp {^sky130_fd_sc_hd__(clkbuf|clkinv)_[0-9]+$} $master -> kind]} {
      set input [$inst findITerm A]
      if {$input eq "NULL"} { error "clock cell [$inst getName] has no A pin" }
      lassign [clock_upstream [$input getNet] $seen] inv depth
      if {$kind eq "clkinv"} { incr inv }
      incr depth
      return [list $inv $depth]
    }
    return [list 0 0]
  }
  return [list 0 0]
}

set buffers [open $::env(CLOCK_LEVEL_BUFFERS_OUT) w]
puts $buffers "instance\tmaster\tlevel\tinput_net"
foreach inst [$block getInsts] {
  set name [$inst getName]
  set master [[$inst getMaster] getName]
  if {![regexp {^sky130_fd_sc_hd__clkbuf_[0-9]+$} $master]} { continue }
  set input [$inst findITerm A]
  if {$input eq "NULL" || [$input getNet] eq "NULL"} { error "clock buffer $name has no connected input" }
  lassign [clock_upstream [$input getNet] [dict create]] _ parent_depth
  puts $buffers "$name\t$master\t[expr {$parent_depth + 1}]\t[[$input getNet] getName]"
}
close $buffers

set sinks [open $::env(CLOCK_LEVEL_SINKS_OUT) w]
puts $sinks "instance\tpin\tmaster\tdepth\tinversions"
foreach inst [$block getInsts] {
  set master [[$inst getMaster] getName]
  foreach iterm [$inst getITerms] {
    set mterm [$iterm getMTerm]
    if {[$mterm getIoType] ne "INPUT"} { continue }
    set pin [$mterm getName]
    set lib_pins [get_lib_pins -quiet */${master}/${pin}]
    if {[llength $lib_pins] != 1 || [get_property [lindex $lib_pins 0] is_register_clock] ne "1"} { continue }
    set net [$iterm getNet]
    if {$net eq "NULL"} { error "sequential sink [$inst getName]/$pin has no net" }
    lassign [clock_upstream $net [dict create]] inv depth
    puts $sinks "[$inst getName]\t$pin\t$master\t$depth\t$inv"
  }
}
close $sinks
puts "CLOCK_TREE_LEVELS buffers=$::env(CLOCK_LEVEL_BUFFERS_OUT) sinks=$::env(CLOCK_LEVEL_SINKS_OUT)"
exit
