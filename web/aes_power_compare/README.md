# CL/OVPS clock power study

A static local website comparing the final routed clock-optimization results for GCD, AES, and RISCV32I. Select a benchmark to update the idle, typical, and peak power results, clock-cell counts, interactive flow explanation, and sign-off details. The Engineering Journal section documents the development path from the placement oracle through CTS restructuring and routed sign-off, then reports the Phase 2 datapath experiments and their current limits. The website presents completed measurements; it does not launch EDA tools.

## Start

On macOS, open `start.command`, or run from the project root:

```sh
python3 web/aes_power_compare/server.py
```

Open <http://127.0.0.1:8765>. Press Control-C in the server terminal to stop it.

## Measurement scope

Power values are routed dynamic power (cell internal plus net switching), measured with OpenSTA from extracted SPEF and matched gate-level VCDs. Leakage is excluded. The clock-cell counts compare the CTS baseline and final routed candidate. Each benchmark has idle, typical, and peak VCD coverage, routed DRC and antenna checks, power-grid checks, timing results, and a Yosys LEC result.

The displayed data is sourced from these final measured sign-off manifests:

- GCD: `build/power/cl_ovps_topology_upgrade_20261103/gcd_cold/measured_signoff_topology_proof/cl_ovps_manifest.json`
- AES: `build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/measured_signoff_graphfilter_aesndr_margin50/cl_ovps_manifest.json`
- RISCV32I: `build/power/cl_ovps_topology_upgrade_20261103/riscv_graph_smoke/measured_signoff_graphfilter/cl_ovps_manifest.json`

The AES result uses its timing-clean 3.8 ns configuration and design-specific CTS NDR settings. RISCV32I uses the 7.0 ns run. All displayed power reductions are measured rather than global-default estimates.

The site labels the three measured Phase 1 candidates as signed off. Phase 2 passes 3C/3D produced no accepted edits in the tested checkpoints; their ablation discussion does not add a new routed power claim. Pass 3E skew statistics are diagnostics only because a matching SDF-annotated VCD was unavailable.
