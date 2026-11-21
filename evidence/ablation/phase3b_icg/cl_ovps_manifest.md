# CL-OVPS signoff: gcd_phase3b_matched_ablation

**Status: FAIL**

Measured candidate ODB: `$CL_OVPS_ROOT/build/orfs/results/sky130hd/gcd_phase3b_cold_20261104/clovps_phase3b_gcd_20261104_route/6_final.odb`

| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |
|---|---:|---:|---:|---:|---:|---:|---:|
| idle | 0.000111186251 W | 0.000152301047 W | 4.11147957e-05 W | -36.978% | -352.88 ps | -27.60 ps | 0 |
| typical | 0.000126086048 W | 0.000163520046 W | 3.74339979e-05 W | -29.689% | -352.88 ps | -27.60 ps | 0 |
| peak | 0.000213727551 W | 0.000243707116 W | 2.99795647e-05 W | -14.027% | -352.88 ps | -27.60 ps | 0 |

## Gates

- PASS — Activity coverage: all pins have measured or clock-propagated activity
- PASS — Measured workload VCD evidence: gate-level VCDs required for workload-specific power signoff
- FAIL — setup/hold regression within guard: each workload compared against baseline slack
- PASS — non-negative setup floor when baseline is closed: candidate setup slack remains non-negative whenever baseline setup slack is non-negative
- FAIL — dynamic power improves in all workloads: internal plus net switching power, routed SPEF/VCD
- PASS — detailed route DRC and antenna: DRC=0; antenna net=0, pin=0
- PASS — VDD/VSS connectivity: VDD=True; VSS=True; PSM-0069=0
- PASS — Yosys LEC: 52 proven, 0 unproven; status=LEC_PASS

## Measurement boundary

OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. This invocation measured existing routed artifacts; it did not rerun optimization or routing.
