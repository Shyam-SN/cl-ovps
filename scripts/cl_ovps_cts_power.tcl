# Per-instance total CTS-cell internal-power oracle after trial CTS.
foreach required {CL_OVPS_ODB CL_OVPS_SDC CL_OVPS_LIBERTY} {
  if {![info exists ::env($required)] || $::env($required) eq ""} {
    error "required environment variable $required is not set"
  }
}
foreach liberty [split $::env(CL_OVPS_LIBERTY) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(CL_OVPS_ODB)
read_sdc $::env(CL_OVPS_SDC)
if {[info exists ::env(CL_OVPS_RC_TCL)] && $::env(CL_OVPS_RC_TCL) ne ""} {
  source $::env(CL_OVPS_RC_TCL)
}
set_thread_count 1
estimate_parasitics -placement
set activity_mode vcd
if {[info exists ::env(CL_OVPS_ACTIVITY_MODE)]} { set activity_mode $::env(CL_OVPS_ACTIVITY_MODE) }
if {$activity_mode eq "vcd"} {
  foreach required {CL_OVPS_VCD CL_OVPS_ACTIVITY_SCOPE} {
    if {![info exists ::env($required)] || $::env($required) eq ""} {
      error "$required is required for VCD activity"
    }
  }
  read_vcd -scope $::env(CL_OVPS_ACTIVITY_SCOPE) $::env(CL_OVPS_VCD)
  puts "CL_OVPS_ACTIVITY_POLICY source=vcd"
} elseif {$activity_mode eq "global_default"} {
  set activity $::env(CL_OVPS_DEFAULT_ACTIVITY)
  set_power_activity -global -activity $activity -duty 0.5
  puts "CL_OVPS_ACTIVITY_POLICY source=global_default activity=$activity duty=0.5"
} else {
  error "unsupported CL_OVPS_ACTIVITY_MODE '$activity_mode'"
}
report_activity_annotation -report_unannotated
set buffers {}
set dummies {}
set inverters {}
set clock_gates {}
set block [ord::get_db_block]
foreach inst [$block getInsts] {
  set instance_name [$inst getName]
  set master [[$inst getMaster] getName]
  if {[string match "clkload*" $instance_name] || [regexp {__clkload_[0-9]+$} $master]} {
    set category dummy
  } elseif {[string match "clkbuf*" $instance_name]} {
    if {[regexp {__(clkinv|inv)_[0-9]+$} $master]} {
      set category inverter
    } elseif {[regexp {__(clkbuf|bufbuf|buf)_[0-9]+$} $master]} {
      set category buffer
    } else {
      error "unrecognized master $master on CTS transport instance $instance_name"
    }
  } else {
    continue
  }
  set sta_cells [get_cells -quiet -hierarchical $instance_name]
  foreach cell $sta_cells {
    switch -- $category {
      dummy { lappend dummies $cell }
      inverter { lappend inverters $cell }
      buffer { lappend buffers $cell }
    }
    puts "CL_OVPS_CTS_POWER_INSTANCE name=[get_full_name $cell] master=[get_property $cell ref_name]"
  }
}
foreach cell [get_cells -quiet -hierarchical *] {
  set master [get_property -quiet $cell ref_name]
  if {[regexp {__(dlclkp|sdlclkp)_[0-9]+$} $master]} {
    lappend clock_gates $cell
    puts "CL_OVPS_CTS_POWER_INSTANCE name=[get_full_name $cell] master=$master"
  }
}
set cells [concat $buffers $dummies $inverters $clock_gates]
if {[llength $cells] == 0} { error "CTS oracle found no clock cells" }
puts "CL_OVPS_CTS_CELLS buffers=[llength $buffers] dummies=[llength $dummies] inverters=[llength $inverters] clock_gates=[llength $clock_gates]"
report_power -instances $cells -digits 12
exit
