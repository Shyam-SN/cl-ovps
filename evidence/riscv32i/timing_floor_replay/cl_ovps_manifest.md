# CL-OVPS signoff: riscv32i_7ns_timingfloor

**Status: PASS**

Measured candidate ODB: `$CL_OVPS_ROOT/build/orfs/results/sky130hd/riscv32i_7ns_clovps/clovps_riscv32i_7ns_timingfloor_route/6_final.odb`

| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |
|---|---:|---:|---:|---:|---:|---:|---:|
| idle | 0.0128568532 W | 0.00960203144 W | -0.00325482176 W | 25.316% | -3.56 ps | +14.05 ps | 0 |
| typical | 0.0184746948 W | 0.0152142257 W | -0.00326046906 W | 17.648% | -3.56 ps | +14.05 ps | 0 |
| peak | 0.0239111213 W | 0.0206587519 W | -0.00325236935 W | 13.602% | -3.56 ps | +14.05 ps | 0 |

## Gates

- PASS — Activity coverage: all pins have measured or clock-propagated activity
- PASS — Measured workload VCD evidence: gate-level VCDs required for workload-specific power signoff
- PASS — setup/hold regression within guard: each workload compared against baseline slack
- PASS — dynamic power improves in all workloads: internal plus net switching power, routed SPEF/VCD
- PASS — detailed route DRC and antenna: DRC=0; antenna net=0, pin=0
- PASS — VDD/VSS connectivity: VDD=True; VSS=True; PSM-0069=0
- PASS — Yosys LEC: 1123 proven, 0 unproven; status=LEC_PASS

## Measurement boundary

OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. This invocation measured existing routed artifacts; it did not rerun optimization or routing.
