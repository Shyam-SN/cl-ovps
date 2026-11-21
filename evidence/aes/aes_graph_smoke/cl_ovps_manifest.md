# CL-OVPS signoff: aes_topology_safe_graphfilter_aesndr_margin50

**Status: PASS**

Measured candidate ODB: `$CL_OVPS_ROOT/build/orfs/results/sky130hd/aes/topology_safe_graphfilter_aesndr_margin50_20261103/6_final.odb`

| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |
|---|---:|---:|---:|---:|---:|---:|---:|
| idle | 0.0165726999 W | 0.0103192204 W | -0.00625347951 W | 37.734% | +43.84 ps | -1.17 ps | 0 |
| typical | 0.0757560544 W | 0.0697000939 W | -0.00605596043 W | 7.994% | +43.84 ps | -1.17 ps | 0 |
| peak | 0.0771465562 W | 0.0711177196 W | -0.00602883659 W | 7.815% | +43.84 ps | -1.17 ps | 0 |

## Gates

- PASS — Activity coverage: all pins have measured or clock-propagated activity
- PASS — Measured workload VCD evidence: gate-level VCDs required for workload-specific power signoff
- PASS — setup/hold regression within guard: each workload compared against baseline slack
- PASS — non-negative setup floor when baseline is closed: candidate setup slack remains non-negative whenever baseline setup slack is non-negative
- PASS — dynamic power improves in all workloads: internal plus net switching power, routed SPEF/VCD
- PASS — detailed route DRC and antenna: DRC=0; antenna net=0, pin=0
- PASS — VDD/VSS connectivity: VDD=True; VSS=True; PSM-0069=0
- PASS — Yosys LEC: 562 proven, 0 unproven; status=LEC_PASS

## Measurement boundary

OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. This invocation measured existing routed artifacts; it did not rerun optimization or routing.
