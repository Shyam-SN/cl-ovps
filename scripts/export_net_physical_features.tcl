# Export placement topology/HPWL and routed wire centerline geometry from an ODB.
# Inputs are provided by the Python driver as FEATURE_ODB, FEATURE_STAGE,
# FEATURE_NET_CSV, and FEATURE_LAYER_CSV.

foreach required {FEATURE_ODB FEATURE_STAGE FEATURE_NET_CSV FEATURE_LAYER_CSV} {
  if {![info exists ::env($required)]} {
    error "required environment variable $required is not set"
  }
}

proc csv_quote {value} {
  set value [string map [list "\"" "\"\""] $value]
  return "\"$value\""
}

proc csv_row {channel values} {
  set encoded {}
  foreach value $values {
    lappend encoded [csv_quote $value]
  }
  puts $channel [join $encoded ","]
}

proc canonical_net_name {name} {
  # OpenDB escapes bus brackets in dbNet names; the SPEF attribution tables
  # use the unescaped signal name.
  return [string map [list {\[} {[} {\]} {]}] $name]
}

read_db $::env(FEATURE_ODB)
set db [ord::get_db]
set chip [$db getChip]
set block [$chip getBlock]
set dbu_per_micron [$block getDbUnitsPerMicron]

set net_csv [open $::env(FEATURE_NET_CSV) w]
set layer_csv [open $::env(FEATURE_LAYER_CSV) w]
csv_row $net_csv {net stage sig_type driver_count sink_count fanout endpoint_count hpwl_dbu bbox_width_dbu bbox_height_dbu dbu_per_micron routed_centerline_dbu via_count has_route}
csv_row $layer_csv {net stage layer centerline_dbu}

set decoder [odb::dbWireDecoder new]
set net_count 0
set routed_net_count 0
set routed_via_count 0

foreach net [$block getNets] {
  set sig_type [$net getSigType]
  if {$sig_type eq "POWER" || $sig_type eq "GROUND"} {
    continue
  }

  set driver_count 0
  set sink_count 0
  set endpoint_count 0
  set xs {}
  set ys {}

  foreach iterm [$net getITerms] {
    set io [$iterm getIoType]
    if {$io eq "OUTPUT" || $io eq "INOUT"} { incr driver_count }
    if {$io eq "INPUT" || $io eq "INOUT"} { incr sink_count }
    set avg [$iterm getAvgXY]
    if {[llength $avg] >= 3 && [lindex $avg 0]} {
      lappend xs [lindex $avg 1]
      lappend ys [lindex $avg 2]
      incr endpoint_count
    }
  }

  foreach bterm [$net getBTerms] {
    set io [$bterm getIoType]
    # BTerm direction is from the block's point of view, opposite to ITerm.
    if {$io eq "INPUT" || $io eq "INOUT"} { incr driver_count }
    if {$io eq "OUTPUT" || $io eq "INOUT"} { incr sink_count }
    set box [$bterm getBBox]
    if {$box ne "NULL"} {
      set x [expr {([$box xMin] + [$box xMax]) / 2}]
      set y [expr {([$box yMin] + [$box yMax]) / 2}]
      lappend xs $x
      lappend ys $y
      incr endpoint_count
    }
  }

  set hpwl 0
  set bbox_width 0
  set bbox_height 0
  if {$endpoint_count > 0} {
    set xmin [lindex $xs 0]
    set xmax $xmin
    set ymin [lindex $ys 0]
    set ymax $ymin
    foreach x $xs y $ys {
      if {$x < $xmin} { set xmin $x }
      if {$x > $xmax} { set xmax $x }
      if {$y < $ymin} { set ymin $y }
      if {$y > $ymax} { set ymax $y }
    }
    set bbox_width [expr {$xmax - $xmin}]
    set bbox_height [expr {$ymax - $ymin}]
    set hpwl [expr {$bbox_width + $bbox_height}]
  }

  set route_by_layer [dict create]
  set via_count 0
  set wire [$net getWire]
  set has_route [expr {$wire ne "NULL"}]
  if {$has_route} {
    incr routed_net_count
    $decoder begin $wire
    set layer ""
    set prev_valid 0
    set prev_x 0
    set prev_y 0
    while {1} {
      set op [$decoder next]
      if {$op == 12} { break }

      # Each explicit route branch starts a new centerline path. A JUNCTION,
      # SHORT, or VWIRE connection is not counted as geometric metal length.
      if {$op == 0 || $op == 1 || $op == 2 || $op == 3} {
        set layer [[$decoder getLayer] getName]
        set prev_valid 0
      } elseif {$op == 4} {
        lassign [$decoder getPoint] x y
        if {$prev_valid && $layer ne ""} {
          set length [expr {abs($x - $prev_x) + abs($y - $prev_y)}]
          dict incr route_by_layer $layer $length
        }
        set prev_x $x
        set prev_y $y
        set prev_valid 1
      } elseif {$op == 5} {
        lassign [$decoder getPoint_ext] x y ext
        if {$prev_valid && $layer ne ""} {
          set length [expr {abs($x - $prev_x) + abs($y - $prev_y)}]
          dict incr route_by_layer $layer $length
        }
        set prev_x $x
        set prev_y $y
        set prev_valid 1
      } elseif {$op == 6 || $op == 7} {
        incr via_count
        set prev_valid 0
      }
    }
  }

  set route_length 0
  set net_name [canonical_net_name [$net getName]]
  dict for {layer length} $route_by_layer {
    incr route_length $length
    csv_row $layer_csv [list $net_name $::env(FEATURE_STAGE) $layer $length]
  }
  incr routed_via_count $via_count
  csv_row $net_csv [list $net_name $::env(FEATURE_STAGE) $sig_type \
    $driver_count $sink_count $sink_count $endpoint_count $hpwl $bbox_width \
    $bbox_height $dbu_per_micron $route_length $via_count $has_route]
  incr net_count
}

close $net_csv
close $layer_csv
puts "FEATURE_EXPORT stage=$::env(FEATURE_STAGE) nets=$net_count routed_nets=$routed_net_count vias=$routed_via_count dbu_per_micron=$dbu_per_micron"
exit
