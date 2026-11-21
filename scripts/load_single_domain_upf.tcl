# Load or validate the single-domain UPF used by the SKY130 GCD/Pico flows.
# Candidate placement ODBs may already contain PD_TOP because the placement
# evaluator read UPF before saving them. Re-running create_power_domain on
# those ODBs emits UPF-0001, so reuse and normalize that domain instead.
proc load_single_domain_upf {upf_file} {
  set domain_name PD_TOP
  set expected_voltage 1.8
  if {![file isfile $upf_file]} {
    error "single-domain UPF file does not exist: $upf_file"
  }

  set block [ord::get_db_block]
  set domain [$block findPowerDomain $domain_name]
  if {$domain eq "NULL"} {
    read_upf -file $upf_file
    set domain [$block findPowerDomain $domain_name]
  }
  if {$domain eq "NULL"} {
    error "UPF did not create the expected power domain $domain_name"
  }

  # The project UPFs currently define one root domain at the nominal SKY130
  # supply. Apply these properties idempotently when the ODB already contains
  # the domain, and mark it as the top domain for downstream UPF consumers.
  if {[lsearch -exact [$domain getElements] "."] < 0} {
    $domain addElement "."
  }
  set_domain_voltage -domain $domain_name -voltage $expected_voltage
  $domain setTop 1

  set voltage [$domain getVoltage]
  set elements [$domain getElements]
  if {abs($voltage - $expected_voltage) > 0.001 || ![$domain isTop]
      || [lsearch -exact $elements "."] < 0} {
    error "UPF verification failed for $domain_name: voltage=$voltage top=[$domain isTop] elements=$elements"
  }
  puts "POWER_DOMAIN_VERIFIED name=$domain_name voltage=$voltage top=1 elements=$elements upf=$upf_file"
}
