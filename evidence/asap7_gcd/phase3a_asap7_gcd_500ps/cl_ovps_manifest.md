# CL-OVPS signoff: asap7-gcd-phase3a-final-signoff

**Status: PASS**

Measured candidate ODB: `$CL_OVPS_ROOT/build/power/phase3a_asap7_gcd_500ps/pass3a_routed_idle_trial_15lib/stage3a.odb`

| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |
|---|---:|---:|---:|---:|---:|---:|---:|
| idle | 0.000147518153 W | 0.000147513352 W | -4.80031304e-09 W | 0.003% | +0.17 ps | +0.00 ps | 0 |
| typical | 0.000259408393 W | 0.00025832012 W | -1.08827226e-06 W | 0.420% | +0.17 ps | +0.00 ps | 0 |
| peak | 0.000408709442 W | 0.000406557912 W | -2.15152977e-06 W | 0.526% | +0.17 ps | +0.00 ps | 0 |

## Gates

- PASS — Activity coverage: all pins have measured or clock-propagated activity
- PASS — Measured workload VCD evidence: gate-level VCDs required for workload-specific power signoff
- PASS — setup/hold regression within guard: each workload compared against baseline slack
- PASS — non-negative setup floor when baseline is closed: candidate setup slack remains non-negative whenever baseline setup slack is non-negative
- PASS — dynamic power improves in all workloads: internal plus net switching power, routed SPEF/VCD
- PASS — detailed route DRC and antenna: DRC=0; antenna net=0, pin=0
- PASS — VDD/VSS connectivity: VDD=True; VSS=True; PSM-0069=0
- PASS — Yosys LEC: 331 proven, 0 unproven; status=LEC_PASS

## Measurement boundary

OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. This invocation measured existing routed artifacts; it did not rerun optimization or routing.
