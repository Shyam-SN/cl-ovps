# Load a supplied UPF only when the ODB does not already contain power domains.
# This keeps the operation idempotent for ORFS checkpoints while avoiding any
# assumptions about the design's power-domain names or nominal voltages.
proc cl_ovps_load_upf {upf_file} {
  if {![file isfile $upf_file]} {
    error "UPF file does not exist: $upf_file"
  }
  set block [ord::get_db_block]
  set domains [$block getPowerDomains]
  if {[llength $domains] == 0} {
    read_upf -file $upf_file
    set domains [$block getPowerDomains]
    if {[llength $domains] == 0} {
      error "UPF loaded but created no power domains: $upf_file"
    }
    set source "file"
  } else {
    set source "odb"
  }
  set names {}
  foreach domain $domains { lappend names [$domain getName] }
  puts "CL_OVPS_UPF source=$source domains=[join [lsort $names] ,] file=$upf_file"
}
