# CL-OVPS Phase 2: Empirical Ablation and Closeout

## Scope and decision

Phase 2 adds three opt-in Stage 3 passes to the validated Phase 1 flow: Liberty-gated sequential-cell sizing (3C), truth-table checked local complex-gate folding (3D), and routed arrival/glitch diagnostics (3E). The runs below preserve the Phase 1 manifests and use the measured Phase 1 sign-off artifacts as Row 0.

No Phase 2 transformation was accepted on the tested benchmarks. Pass 3C found no eligible sequential instances. Pass 3D found no GCD mappings and rejected every exercised AES/RISCV32I trial under the configured physical/timing checks. Therefore Rows 1–3 are identity ablations: their power columns carry forward the existing Row 0 measurement because no netlist edit was committed. No new route or workload simulation was necessary for a no-edit output. These are zero incremental Phase 2 gains, not evidence that no future candidate or benchmark can benefit.

Pass 3E produced routed arrival-skew distributions and SDF files. The required matched zero-delay/SDF gate-level VCD pairs were unavailable, so the glitch-transition fraction is unmeasured and the experimental alignment mutation remains disabled.

## Row 0: Phase 1 measured baseline and candidate power

All values are OpenSTA dynamic power with routed SPEF and measured gate-level VCD. Power is in mW; each delta is candidate minus baseline, so a negative delta is a reduction. Percentages use baseline dynamic power. Leakage is excluded from dynamic power.

| Benchmark | Workload | Baseline internal | Candidate internal | Δ internal | Baseline switching | Candidate switching | Δ switching | Baseline dynamic | Candidate dynamic | Δ dynamic | Dynamic reduction |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| GCD | Idle | 0.110017 | 0.083887 | −0.026130 | 0.025616 | 0.027299 | +0.001683 | 0.135633 | 0.111186 | −0.024447 | 18.02% |
| GCD | Typical | 0.118589 | 0.092225 | −0.026364 | 0.032180 | 0.033861 | +0.001681 | 0.150769 | 0.126086 | −0.024683 | 16.37% |
| GCD | Peak | 0.168578 | 0.140498 | −0.028080 | 0.071463 | 0.073229 | +0.001766 | 0.240041 | 0.213728 | −0.026314 | 10.96% |
| AES | Idle | 12.103504 | 6.953467 | −5.150037 | 4.469196 | 3.365754 | −1.103443 | 16.572700 | 10.319220 | −6.253480 | 37.73% |
| AES | Typical | 34.071788 | 29.054934 | −5.016854 | 41.684266 | 40.645160 | −1.039106 | 75.756054 | 69.700094 | −6.055960 | 7.99% |
| AES | Peak | 34.596764 | 29.606873 | −4.989890 | 42.549793 | 41.510846 | −1.038946 | 77.146556 | 71.117720 | −6.028837 | 7.81% |
| RISCV32I | Idle | 10.288409 | 7.185904 | −3.102505 | 2.568444 | 2.416128 | −0.152317 | 12.856853 | 9.602031 | −3.254822 | 25.32% |
| RISCV32I | Typical | 12.361262 | 9.251282 | −3.109980 | 6.113433 | 5.962944 | −0.150489 | 18.474695 | 15.214226 | −3.260469 | 17.65% |
| RISCV32I | Peak | 14.588240 | 11.478559 | −3.109680 | 9.322882 | 9.180193 | −0.142689 | 23.911121 | 20.658752 | −3.252369 | 13.60% |

## Required ablation rows

The table shows reduction from the same Phase 1 baseline for each row. Rows 1–3 are identity results where the relevant pass had zero committed edits. They inherit the routed Row 0 power measurement; they are not separate reroute experiments.

| Benchmark | Workload | Row 0: Phase 1 | Row 1: +3C | Row 2: +3D | Row 3: +3C+3D |
|---|---|---:|---:|---:|---:|
| GCD | Idle | 24.447 µW | 24.447 µW (no-op) | 24.447 µW (no-op) | 24.447 µW (no-op) |
| GCD | Typical | 24.683 µW | 24.683 µW (no-op) | 24.683 µW (no-op) | 24.683 µW (no-op) |
| GCD | Peak | 26.314 µW | 26.314 µW (no-op) | 26.314 µW (no-op) | 26.314 µW (no-op) |
| AES | Idle | 6.253480 mW | 6.253480 mW (no-op) | 6.253480 mW (no accepted fold) | 6.253480 mW (no-op) |
| AES | Typical | 6.055960 mW | 6.055960 mW (no-op) | 6.055960 mW (no accepted fold) | 6.055960 mW (no-op) |
| AES | Peak | 6.028837 mW | 6.028837 mW (no-op) | 6.028837 mW (no accepted fold) | 6.028837 mW (no-op) |
| RISCV32I | Idle | 3.254822 mW | 3.254822 mW (no-op) | 3.254822 mW (no accepted fold) | 3.254822 mW (no-op) |
| RISCV32I | Typical | 3.260469 mW | 3.260469 mW (no-op) | 3.260469 mW (no accepted fold) | 3.260469 mW (no-op) |
| RISCV32I | Peak | 3.252369 mW | 3.252369 mW (no-op) | 3.252369 mW (no accepted fold) | 3.252369 mW (no-op) |

### Pass 3C: sequential-cell Liberty audit

The Liberty parser audited all 69 sequential master types and found all 69 clock/gate pins from `clocked_on`, latch `enable`, and Liberty clock-gating pin attributes. It found 30 replacement-master pairs that met the static non-increasing capacitance filter on every clock pin. The first audit version matched only a literal `CLK` pin; after catching that gap, the parser and Pass 3C were corrected and rerun on all three benchmarks. The updated table records `CLK_N`, latch `GATE`, and integrated clock-gate clock pins explicitly. Pass 3C still found zero instance candidates satisfying the same-semantic-class and operating-point power checks. Each corrected output ODB SHA-256 matched its input, so Pass 3C caused exactly zero routed power or timing change.

The corrected characterization artifact is [stage3c_liberty_sequential_audit.tsv](stage3c_liberty_sequential_audit.tsv). Final per-design eligibility and hashes are in `stage3c_manifest.json` under `build/power/cl_ovps_phase2_final_20261104/{gcd,aes,riscv32i}_pass3c_final/`.

### Pass 3D: local complex-gate folds

Truth tables were built from Liberty function attributes for 270 combinational functions. The scan used a 15 µm locality ceiling, same/adjacent-row legality, VCD activity threshold, single-fanout intermediate nets, and exclusions for clock/reset/set/scan/interface and physical-only structures. Candidate edits ran in isolated OpenROAD trials and required physical checks, timing/power guards, and immediate graph-aware LEC. None was promoted.

| Benchmark | Preflight chains rejected | Candidate mappings | OpenROAD fold trials | Outcome |
|---|---:|---:|---:|---|
| GCD | 77 | 0 | 0 | No candidate mapping |
| AES | 5,383 | 416 | 41 | 41 failed the setup floor; 23 additional mappings in the bounded prefix were wider than the downstream legal site; remaining 352 proposals were logged as `TRIAL_LIMIT` |
| RISCV32I | 1,765 | 85 | 85 | 51 trials failed the setup floor; 18 were wider than the downstream site; 12 had no intermediate net; 4 mapped-net checks failed |

Preflight rejection counts are retained exactly in each `stage3d_rejections.tsv`: GCD — 32 `NON_SCALAR_OR_UNCONNECTED_INPUT`, 25 `COMPOSITE_INPUT_COUNT_OVER_4`, 14 `LOW_ACTIVITY_OR_UNKNOWN`, 5 `PHYSICAL_BBOX_OVER_15UM`, 1 `TESTBENCH_INTERFACE_NET`; AES — 3,721 `COMPOSITE_INPUT_COUNT_OVER_4`, 795 `NO_COMPLEX_EQUIVALENT`, 577 `PHYSICAL_BBOX_OVER_15UM`, 125 `NON_SCALAR_OR_UNCONNECTED_INPUT`, 122 `NOT_SAME_OR_ADJACENT_ROW`, 40 `LOW_ACTIVITY_OR_UNKNOWN`, 2 `RESET_SET_OR_SCAN_TREE`, 1 `TESTBENCH_INTERFACE_NET`; RISCV32I — 1,011 `COMPOSITE_INPUT_COUNT_OVER_4`, 256 `PHYSICAL_BBOX_OVER_15UM`, 204 `LOW_ACTIVITY_OR_UNKNOWN`, 187 `NO_COMPLEX_EQUIVALENT`, 74 `NOT_SAME_OR_ADJACENT_ROW`, 33 `NON_SCALAR_OR_UNCONNECTED_INPUT`. RISC-V trial mapping failures were three `MAPPED_NET_MISSING:dp.memsrcmux.d1[4]` and one `MAPPED_NET_MISSING:dp.alu.exor[7]`, in addition to 12 `INTERMEDIATE_NET_MISSING`. Exact per-trial rejection reasons are in `stage3d_trial_report.tsv`.

The AES and RISCV32I scans started at the correct pre-route Stage 3 checkpoints. Their baseline WNS at those checkpoints was negative (AES −31.66 ps; RISCV32I approximately −86.24 ps), so all trial edits failed the configured non-negative setup floor. Both scan output ODB hashes equal their input Stage 3 ODB hashes. An earlier AES scan against a post-route ODB is invalid for attribution and is excluded here; edits to a routed database leave stale route geometry, so that run was not used as a candidate result.

### Pass 3E: arrival and glitch diagnostics

| Benchmark | Gate-arrival rows | Min skew (ns) | Median (ns) | P95 (ns) | Max (ns) | SDF/VCD glitch result | Alignment edit |
|---|---:|---:|---:|---:|---:|---|---|
| GCD | 104 | 0.008502 | 1.481442 | 4.140191 | 4.290466 | Pending; no matched SDF VCD | Disabled |
| AES | 6,083 | 0.000648 | 0.335973 | 0.982217 | 2.446854 | Pending; no matched SDF VCD | Disabled |
| RISCV32I | 1,824 | 0.002060 | 1.755371 | 4.671805 | 9.142217 | Pending; no matched SDF VCD | Disabled |

The routed SDF and read-only timing diagnostics were generated for all three benchmarks. The attempted Icarus SDF replay did not resolve escaped flattened instance paths in the OpenROAD SDF (for example, the `dpath/b_reg/out[9]` path), so no timing-annotated VCD pair could be validated. Consequently, the glitch transition count/fraction is `null` in each manifest. No alignment or delay-padding edit was made.

## Sign-off and promotion

Phase 1 remains the only promoted optimization. Its measured manifests pass the existing benchmark gates: zero VCD-unannotated pins, zero detailed-route DRC/antenna violations, VDD/VSS checks pass, and LEC proves GCD 52/52, AES 562/562, and RISCV32I 1,123/1,123. The Phase 2 trial outputs were isolated; none overwrote the Phase 1 routed ODB, SPEF, VCD, or sign-off manifest.

Phase 2 does **not** satisfy the promotion rule for a new datapath transform: no changed candidate was routed, so no positive incremental routed saving was established on the held-out benchmarks. The identity rows show zero incremental change because the accepted implementation ODB stayed unchanged; they are not separate reroute measurements. Passes 3C and 3D remain opt-in. Pass 3E remains a read-only diagnostic; experimental alignment stays disabled until matched SDF and zero-delay VCDs show a nonzero glitch overhead and the candidate passes full sign-off.

## Reproduction commands and evidence

All commands are run from the repository root. The following are the full bounded pre-route Pass 3D holdout commands used for AES and RISCV32I:

```sh
python3 scripts/cl_ovps_pass3d_gate_folding.py \
  --input-odb build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/stage3_graphfilter.odb \
  --input-netlist build/power/cl_ovps_phase2_final_20261104/aes_pass3d_preroute_stage3.v \
  --top aes_cipher_top \
  --sdc build/orfs/results/sky130hd/aes/topology_safe_graphfilter_aesndr_margin50_20261103/4_cts.sdc \
  --vcd build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/aes_activity_graphfilter_aesndr_margin50/topology_safe_graphfilter_aesndr_margin50_20261103/typical/aes_activity.vcd \
  --scope tb_aes_activity/dut \
  --lib openroad/test/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib \
  --rc-tcl tools/openroad-flow-scripts/flow/platforms/sky130hd/setRC.tcl \
  --lef openroad/test/sky130hd/sky130_fd_sc_hd__nom.tlef \
  --lef openroad/test/sky130hd/sky130_fd_sc_hd.lef \
  --clock-period-ns 3.8 --max-trials 64 \
  --output-dir build/power/cl_ovps_phase2_final_20261104/aes_pass3d_preroute_trial64

python3 scripts/cl_ovps_pass3d_gate_folding.py \
  --input-odb build/power/cl_ovps_topology_upgrade_20261103/riscv_graph_smoke/candidate_stage3_graphfilter.odb \
  --input-netlist build/power/cl_ovps_phase2_final_20261104/riscv32i_pass3d_preroute_stage3.v \
  --top riscv \
  --sdc build/orfs/results/sky130hd/riscv32i_7ns_clovps/topology_safe_graphfilter_20261103/4_cts.sdc \
  --vcd build/power/cl_ovps_topology_upgrade_20261103/riscv_graph_smoke/paired_sim_graphfilter/paired_typical.vcd \
  --scope tb_riscv32i_vcd/dut_candidate \
  --lib openroad/test/sky130hd/sky130_fd_sc_hd__tt_025C_1v80.lib \
  --rc-tcl tools/openroad-flow-scripts/flow/platforms/sky130hd/setRC.tcl \
  --lef openroad/test/sky130hd/sky130_fd_sc_hd__nom.tlef \
  --lef openroad/test/sky130hd/sky130_fd_sc_hd.lef \
  --clock-period-ns 7.0 --max-trials 85 \
  --output-dir build/power/cl_ovps_phase2_final_20261104/riscv32i_pass3d_preroute_trial85
```

The Stage 3C/3D/3E modules expose their exact per-design arguments through `python3 scripts/cl_ovps_pass3c_seq_sizing.py --help`, `python3 scripts/cl_ovps_pass3d_gate_folding.py --help`, and `python3 scripts/cl_ovps_pass3e_glitch_diag.py --help`. To inject 3C/3D into a configured Stage 3 flow, use `run_cl_ovps_universal.py --flow_plan <plan.json> --enable-pass-3c --enable-pass-3d --pass3d-max-trials 64`; Pass 3D requires LEFs and remains opt-in. For post-route diagnostics, `run_cl_ovps_universal.py --run-3e-diagnostic --sdf_vcd_dir <matched-sdf-vcd-directory>` compares its measured typical VCD with the timing-annotated typical VCD. The standalone read-only diagnostic instead accepts `--zero-delay-vcd <vcd>` and `--sdf-vcd <vcd>` plus the matching `--scope`.

Primary evidence lives in `build/power/cl_ovps_phase2_final_20261104/`, including per-pass manifests, input/output hashes, rejected proposal tables, trial reports, arrival-skew TSVs, and routed SDF files. Phase 1 measured workload manifests are linked in the closeout report and remain authoritative for Row 0.
