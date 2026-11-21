# CL-OVPS signoff: gcd_topology_safe_20261103

**Status: PASS**

Measured candidate ODB: `$CL_OVPS_ROOT/build/orfs/results/sky130hd/gcdtopo_safe_20261103/route/6_final.odb`

| Workload | Baseline dynamic | Candidate dynamic | Delta | Reduction | Δ setup | Δ hold | VCD missing |
|---|---:|---:|---:|---:|---:|---:|---:|
| idle | 0.000135632923 W | 0.000111186251 W | -2.44466719e-05 W | 18.024% | +216.30 ps | -3.95 ps | 0 |
| typical | 0.000150769112 W | 0.000126086048 W | -2.46830641e-05 W | 16.371% | +216.30 ps | -3.95 ps | 0 |
| peak | 0.000240041059 W | 0.000213727551 W | -2.6313508e-05 W | 10.962% | +216.30 ps | -3.95 ps | 0 |

## Gates

- PASS — Activity coverage: all pins have measured or clock-propagated activity
- PASS — Measured workload VCD evidence: gate-level VCDs required for workload-specific power signoff
- PASS — setup/hold regression within guard: each workload compared against baseline slack
- PASS — non-negative setup floor when baseline is closed: candidate setup slack remains non-negative whenever baseline setup slack is non-negative
- PASS — dynamic power improves in all workloads: internal plus net switching power, routed SPEF/VCD
- PASS — detailed route DRC and antenna: DRC=0; antenna net=0, pin=0
- PASS — VDD/VSS connectivity: VDD=True; VSS=True; PSM-0069=0
- PASS — Yosys LEC: 52 proven, 0 unproven; status=LEC_PASS

## Measurement boundary

OpenROAD re-read the supplied final ODB, SDC, SPEF, Liberty, and each workload VCD. This invocation measured existing routed artifacts; it did not rerun optimization or routing.
