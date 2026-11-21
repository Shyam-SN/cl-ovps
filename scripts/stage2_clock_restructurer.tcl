# Stage 2 wrapper for the Sky130HD post-CTS clock restructuring pass.
# Set CLOCK_RESTRUCTURE_ACTION to prune, replace, or all before invoking.
set action all
if {[info exists ::env(CLOCK_RESTRUCTURE_ACTION)]} {
  set action $::env(CLOCK_RESTRUCTURE_ACTION)
}
switch -- $action {
  prune { set ::env(AUDIT_PRUNE_DUMMIES) 1; set ::env(AUDIT_SWAP_CLOCK_BUFFERS) 0 }
  replace { set ::env(AUDIT_PRUNE_DUMMIES) 0; set ::env(AUDIT_SWAP_CLOCK_BUFFERS) 1 }
  all { set ::env(AUDIT_PRUNE_DUMMIES) 1; set ::env(AUDIT_SWAP_CLOCK_BUFFERS) 1 }
  default { error "unsupported CLOCK_RESTRUCTURE_ACTION '$action'" }
}
set ::env(AUDIT_DOWNSIZE) 0
set ::env(AUDIT_PIN_SWAP) 0
source [file join [file dirname [info script]] cl_ovps_sky130hd_stage23.tcl]
