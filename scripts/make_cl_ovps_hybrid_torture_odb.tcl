# Create a small live-ODB hybrid clock topology from the routed GCD CTS stage.
# Root buffer drives a direct flop tap and a new parent->child buffer star.
foreach required {CL_OVPS_ODB CL_OVPS_SDC CL_OVPS_LIBERTY CL_OVPS_TORTURE_OUT} {
  if {![info exists ::env($required)] || $::env($required) eq ""} {
    error "required environment variable $required is not set"
  }
}
foreach liberty [split $::env(CL_OVPS_LIBERTY) "\n"] {
  if {$liberty ne ""} { read_liberty $liberty }
}
read_db $::env(CL_OVPS_ODB)
read_sdc $::env(CL_OVPS_SDC)
set block [ord::get_db_block]

set root [$block findInst clkbuf_0_clk]
set parent [$block findInst clkbuf_2_0__f_clk]
set child [$block findInst clkbuf_2_1__f_clk]
set tapped_leaf [$block findInst clkbuf_2_2__f_clk]
foreach {name inst} [list root $root parent $parent child $child tapped_leaf $tapped_leaf] {
  if {$inst eq "NULL"} { error "torture fixture requires $name clock cell" }
}

set root_out [$root findITerm X]
set parent_in [$parent findITerm A]
set parent_out [$parent findITerm X]
set child_in [$child findITerm A]
set child_out [$child findITerm X]
set tap_out [$tapped_leaf findITerm X]
foreach {name iterm} [list root_out $root_out parent_in $parent_in parent_out $parent_out child_in $child_in child_out $child_out tap_out $tap_out] {
  if {$iterm eq "NULL" || [$iterm getNet] eq "NULL"} { error "torture fixture has no net at $name" }
}
set root_net [$root_out getNet]
set old_parent_net [$parent_out getNet]
set child_net [$child_out getNet]
set tap_net [$tap_out getNet]

# Insert an intermediate parent on the branch to child, preserving all original
# sink endpoints. This creates a legal lower star while root remains a hybrid.
set hybrid_net [odb::dbNet_create $block cl_ovps_torture_parent_net]
odb::dbITerm_disconnect $parent_out
odb::dbITerm_connect $parent_out $hybrid_net
odb::dbITerm_disconnect $child_in
odb::dbITerm_connect $child_in $hybrid_net

# Move every original parent-branch load onto the child's output net.
foreach iterm [$old_parent_net getITerms] {
  set inst [$iterm getInst]
  set pin [[$iterm getMTerm] getName]
  if {[$inst getName] eq [$parent getName] && $pin eq "X"} { continue }
  odb::dbITerm_disconnect $iterm
  odb::dbITerm_connect $iterm $child_net
}

# Add a direct sink tap to root output. Stage 2 must reject the root star, but
# still permit the untapped parent->child star above it.
set direct_tap NULL
foreach iterm [$tap_net getITerms] {
  set inst [$iterm getInst]
  set master [[$inst getMaster] getName]
  set pin [[$iterm getMTerm] getName]
  if {[regexp {__(e?df|sdf|dfr|dfst)[A-Za-z0-9_]*$} $master] && $pin in {CLK CLK_N}} {
    set direct_tap $iterm
    break
  }
}
if {$direct_tap eq "NULL"} { error "could not find a sequential sink for the direct root tap" }
odb::dbITerm_disconnect $direct_tap
odb::dbITerm_connect $direct_tap $root_net

write_db $::env(CL_OVPS_TORTURE_OUT)
set tap_inst [$direct_tap getInst]
puts "CL_OVPS_TORTURE_ODB hybrid_root_tap=[$tap_inst getName]/[[$direct_tap getMTerm] getName] legal_star=[$parent getName]->[$child getName]"
exit
