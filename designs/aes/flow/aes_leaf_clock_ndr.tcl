# Apply the CL-OVPS 1x-width / 2x-spacing NDR to all post-CTS leaf clock nets.
set block [ord::get_db_block]
set ndr_name AES_CLK_LEAF_1W_2S
if {[$block findNonDefaultRule $ndr_name] eq "NULL"} {
  create_ndr -name $ndr_name -width {*1} -spacing {*2}
}

set leaf_count 0
foreach net [$block getNets] {
  set name [$net getName]
  if {[regexp {^clknet_.*__leaf_clk$} $name] ||
      [regexp {^clknet_leaf_[0-9]+_clk$} $name]} {
    assign_ndr -ndr $ndr_name -net $name
    incr leaf_count
  }
}
if {$leaf_count == 0} {
  error "CL-OVPS leaf clock NDR matched no clock nets"
}
puts "AES_LEAF_CLOCK_NDR name=AES_CLK_LEAF_1W_2S width_factor=1 spacing_factor=2 nets=$leaf_count"
