# Stage 3 wrapper for timing-, slew-, and VCD-power-guarded Sky130HD optimization.
# Set VCD_OPT_ACTION to downsize, pinswap, or all before invoking.
set action all
if {[info exists ::env(VCD_OPT_ACTION)]} {
  set action $::env(VCD_OPT_ACTION)
}
switch -- $action {
  downsize { set ::env(AUDIT_DOWNSIZE) 1; set ::env(AUDIT_PIN_SWAP) 0 }
  pinswap { set ::env(AUDIT_DOWNSIZE) 0; set ::env(AUDIT_PIN_SWAP) 1 }
  all { set ::env(AUDIT_DOWNSIZE) 1; set ::env(AUDIT_PIN_SWAP) 1 }
  default { error "unsupported VCD_OPT_ACTION '$action'" }
}
set ::env(AUDIT_PRUNE_DUMMIES) 0
set ::env(AUDIT_SWAP_CLOCK_BUFFERS) 0
set ::env(AUDIT_DOWNSIZE_MODE) budget
if {![info exists ::env(AUDIT_DOWNSIZE_BUDGET_PS)]} { set ::env(AUDIT_DOWNSIZE_BUDGET_PS) 50.0 }
if {![info exists ::env(AUDIT_DOWNSIZE_MAX_CELLS)]} { set ::env(AUDIT_DOWNSIZE_MAX_CELLS) 40 }
if {![info exists ::env(AUDIT_DOWNSIZE_MIN_SLACK_PS)]} { set ::env(AUDIT_DOWNSIZE_MIN_SLACK_PS) 50.0 }
source [file join [file dirname [info script]] cl_ovps_sky130hd_stage23.tcl]
