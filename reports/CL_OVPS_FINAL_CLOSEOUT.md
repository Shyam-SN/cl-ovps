# CL-OVPS Project Closeout

**Status: GCD, AES, and RISCV32I pass measured signoff under their timing gates.** The latest AES reroute, using its design-specific 107-net leaf-clock NDR plus the 0.5 ns setup-repair margin, improves setup slack from −41.947 ps to **+1.891 ps** (+43.839 ps). GCD and RISCV32I also have positive setup slack. On the 7.0 ns RISC-V run, a timing-aware Stage 3 replay restored the single timing-sensitive `a21o` resize; routed setup WNS is +15.230 ps baseline and +11.674 ps candidate, and hold improves by +14.051 ps.

**Original GCD/AES/RISCV32I sign-off scope: 100% complete.** The topology-safe extension is validated on these three Sky130HD benchmarks and synthetic edge cases; it is not a universal guarantee for arbitrary PDKs, clocking schemes, or UPF designs.

## Frozen plan and decision

- Keep the AES clock target at **3.8 ns**. No SDC relaxation was made.
- Use routed SPEF plus OpenSTA/VCD for power conclusions.
- Keep the **Tier 1 CTS total-clock-cell oracle** as the placement objective.
- Keep **Tier 2 pre-route switching prediction retired**; it did not meet precision requirements on the holdout.
- Require zero unannotated VCD pins, zero DRC/antenna violations, 100% LEC, and no more than 50 ps routed setup/hold regression.

## GCD: completed end-to-end result

The automated multi-stage run produced these routed dynamic-power changes versus baseline:

| Workload | Power change | Relative change |
|---|---:|---:|
| Idle | −24.187 µW | — |
| Typical | −24.438 µW | −16.21% |
| Peak | −26.333 µW | — |

Typical-power attribution was Stage 1 placement **−9.341 µW**, Stage 2A dummy pruning **−1.476 µW**, Stage 2B clock-tree replacement **−13.230 µW**, and Stages 3A/3B combined **−0.391 µW**. Setup slack improved by **205.884 ps**; hold changed by **−2.671 ps**. VCD annotation had zero missing pins, detailed-route DRC/antenna counts were zero, and Yosys proved **51/51** equivalence points.

## AES: routed timing-clean candidate

The original full Stage 1+2+3 candidate, `aes_power38_margin50_20261102a_xrow39070_stage3b_corrected`, failed the setup guard. The final candidate is `aes_power38_margin50_20261103a_xrow39070_stage2pathresize_xor3_2`: it retains the selected Tier 1 placement and Stage 2 clock restructuring, excludes Stage 3 downsizing/pin swapping, and changes `_12642_` to the legal `sky130_fd_sc_hd__xor3_2` master. All power values below are routed SPEF/VCD dynamic power from OpenSTA.

| Workload | Baseline | Timing-clean candidate | Change | Reduction |
|---|---:|---:|---:|---:|
| Idle | 16.572700 mW | 10.319220 mW | −6.253480 mW | 37.73% |
| Typical | 75.756054 mW | 69.700094 mW | −6.055960 mW | 7.99% |
| Peak | 77.146556 mW | 71.117720 mW | −6.028837 mW | 7.82% |

Clock-cell internal power fell by **5.143709 mW**. The clock tree changed from 109 buffers and 97 dummy cells to 8 buffers, 0 dummy cells, and 100 inverters. All 107 leaf clock nets retain the required `AES_CLK_LEAF_1W_2S` non-default rule.

**Passed AES checks:** routed setup slack improved from **−41.947 ps** to **+1.891 ps** (**+43.839 ps**); hold changed from **462.804 ps** to **461.629 ps** (**−1.175 ps**). The candidate passes the ≤50 ps setup/hold regression guard and has nonnegative setup slack. Detailed-route DRC is **0**, antenna net/pin violations are **0**, VCD unannotated pins are **0** for all three workloads, and all 107 leaf clock NDR assignments are present. The final routed ODB passes both VDD and VSS `check_power_grid` connectivity.

**Formal status:** the exact corrected candidate passes Yosys LEC: **562/562 equivalence points proven**, and `equiv_status -assert` reports “Equivalence successfully proven.” The proof uses `equiv_simple -seq 1 -short` followed by `equiv_induct -seq 16`; it normalizes CTS-only instances, maps both designs' clock pins to a common clock, and ties omitted standard-cell VPWR/VGND/body pins to ideal functional rails. It uses the Sky130 functional models plus behavioral mux/power-good UDP models.

**LEC diagnosis:** the earlier unresolved-point runs were caused by an overbroad clock abstraction that removed every `clkinv` instance, including synthesized datapath inverters. It also left physically omitted supply pins and a 4:1 mux UDP unmodeled. That damaged the proof model and produced the misleading 648/247 unresolved state points. The corrected normalizer removes only CTS instances with the `clkbuf...` instance prefix, preserves datapath inverters, and supplies the required functional cell models. No register-name remapping was necessary.

**Power-grid diagnosis and fix:** the original candidate passes `check_power_grid` at `3_place` but fails both VDD and VSS after CTS. The illegal-site hypothesis was false: placement checks passed, the affected instances were on-grid, and `SPECIALNETS` were unchanged. The sufficient trigger was `_12642_`, a datapath XOR3 upsized from `sky130_fd_sc_hd__xor3_1` to `_xor3_4`, which overlapped a cell-master obstruction. `_xor3_1` cleared PSM but cost about 30 ps setup and made slack negative. The final candidate uses `_xor3_2`; fresh detailed routing and final ODB checks pass both VDD and VSS connectivity. PSM-0043 indicates a supply shape/cell-obstruction overlap, not necessarily an electrically floating rail.

## Plan completion by workstream

| Workstream | Status |
|---|---|
| Power-mechanism discovery and Tier 2 retirement | Complete |
| Tier 1 CTS-oracle placement optimizer | Complete; validated on GCD and applied to AES candidate |
| GCD automated multi-stage optimization | Complete; routed, measured, and LEC-clean |
| AES 3.8 ns baseline and routed power comparison | Complete as an experiment; baseline has negative slack |
| AES routed physical checks and VCD coverage | Complete; zero DRC/antenna and zero unannotated pins |
| AES routed LEC | **Pass; exact corrected candidate 562/562** |
| AES setup/hold closure and <=50 ps regression | **Pass** |
| AES detailed-route DRC/antenna and VCD coverage | **Pass** |
| AES gate-level differential simulation | **Pass for tested typical and peak transaction sequences** |
| AES VDD/VSS power-grid connectivity | **Pass on fresh `_xor3_2` routed candidate** |
| RISCV32I 7.0 ns cold measured workload power | **Pass; three VCD workloads, zero missing pins, 3.260 mW typical savings** |
| RISCV32I route, PDN, and LEC | **Pass; 0 DRC/antenna, VDD/VSS connected, 1,123/1,123 LEC** |
| RISCV32I setup timing | **Pass; +15.230 ps baseline WNS to +11.674 ps candidate WNS, within the 50 ps guard** |

## Closeout decision

GCD, the corrected AES candidate, and the timing-repaired RISC-V candidate pass the measured signoff gates. The RISC-V candidate has positive routed setup and hold slack, improves dynamic power in all three measured workloads, has zero unannotated VCD pins, zero DRC/antenna violations, passing VDD/VSS connectivity, and 1,123/1,123 LEC points.

## Evidence files

- GCD routed attribution: `build/power/gcd_auto_multistage_20261101a/routed_stage_attribution.tsv`
- GCD final equivalence: `build/power/gcd_auto_multistage_20261101a/lec_no_inames/equivalence.log`
- AES corrected routed power summary: `build/power/aes_multistage_clean_ndr_20261102a/xor3_2_routed_reports/aes_routed_power_summary.tsv`
- Latest AES topology-safe measured signoff: `build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/measured_signoff_graphfilter_aesndr_margin50/cl_ovps_manifest.json`
- AES design-specific NDR route and Stage 4 recheck: `designs/aes/flow/aes_leaf_clock_ndr.tcl`, `build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/stage4_graphfilter_aesndr_margin50_recheck/stage4_manifest.json`
- Cold AES/RISCV Stage 2 audits: `build/power/cl_ovps_topology_upgrade_20261103/aes_graph_smoke/cold_stage2_raw_cts/clock.log`, `build/power/cl_ovps_topology_upgrade_20261103/riscv_graph_smoke/cold_stage2_raw_cts/clock.log`
- AES corrected detailed-route log: `build/orfs/logs/sky130hd/aes/aes_power38_margin50_20261103a_xrow39070_stage2pathresize_xor3_2/5_2_route.log`
- AES corrected final SPEF, netlist, and ODB: `build/orfs/results/sky130hd/aes/aes_power38_margin50_20261103a_xrow39070_stage2pathresize_xor3_2/6_final.spef`, `6_final.v`, `6_final.odb`
- AES exact-candidate LEC pass: `build/power/aes_multistage_clean_ndr_20261102a/analysis/lec_xor3_2_official/status.txt` and `equivalence.log` (562/562)
- AES LEC normalization and UDP models: `scripts/run_aes_netlist_lec.py` and `scripts/run_gcd_netlist_lec.py`
- AES baseline/candidate differential simulation: `build/power/aes_multistage_clean_ndr_20261102a/analysis/functional_diff_final/`
- AES PSM source interpretation: `tools/openroad/src/psm/src/ir_solver.cpp` (`getMasterObstructions` / `findShorts`) and `tools/openroad/src/psm/src/ir_short.cpp`
- AES final PSM check: `build/power/aes_multistage_clean_ndr_20261102a/analysis/psm_final_xor3_2_routed/run.log`

## Post-closeout generalization update

The original 100% closeout above covers the frozen GCD/AES scope. Follow-up work tested cold execution on a third design and unified the generic LEC path across hierarchical mapped netlists.

| Design | Latest evidence | Status |
|---|---|---|
| GCD | Cold replay from `3_place.odb`: measured routed dynamic-power reductions of 24.683 µW idle, 24.860 µW typical (16.49%), and 26.208 µW peak. Timing, route, PDN, annotation, and LEC gates passed. | Measured signoff |
| AES | Corrected `_xor3_2` candidate: 6.253480 mW idle, 6.055960 mW typical (7.99%), and 6.028837 mW peak savings; +1.891 ps final setup slack, zero DRC/antenna/VCD drops, VDD/VSS pass, and 562/562 generic LEC. | Measured signoff |
| RISCV32I | Fresh 7.0 ns cold run and timing-floor reroute: 3.254822 mW idle (25.316%), 3.260469 mW typical (17.648%), and 3.252369 mW peak (13.602%) savings. 1,123/1,123 LEC, zero DRC/antenna, zero VCD drops, VDD/VSS pass. Setup changes from +15.230 ps to +11.674 ps (−3.556 ps); hold improves by +14.051 ps. | Measured signoff; positive setup slack |

The earlier RISC-V global-default numbers and 7.0 ns STA-only re-evaluation were preliminary. They are superseded by the fresh 7.0 ns routed experiment below, which uses paired gate-level workload VCDs and extracted SPEFs for both baseline and candidate.

The shared adapter at `scripts/run_cl_ovps_gate_lec.py` handles the tested flat mapped GCD, AES, and RISC-V gate-level netlists (generated from hierarchical RTL) through one flow, proving 51/51, 562/562, and 1,123/1,123 points. It namespaces module definitions, flattens each proof side independently, validates visible clock endpoint/source/parity, abstracts only verified CTS transport, maps supported Sky130HD DFF strengths, and ties omitted supply pins. Nested hierarchical clock paths fail closed. The RISC-V asynchronous-reset proof uses Yosys `async2sync`, so reset behavior between clock edges is not modeled. Multi-clock/generated-clock designs, integrated clock gates, macros, and broader UPF behavior remain outside measured benchmark coverage; the synthetic topology proof exercises ICG, negedge, and mux cases.

## RISCV32I: fresh 7.0 ns measured run

The baseline and candidate were both routed at 7.0 ns. The SDC uses 1.20 ns input delays and 1.15 ns output delays. This 50 ps output-budget adjustment was made after the 1.20 ns output-delay baseline showed −11.19 ps setup WNS; the selected baseline closes at +15.230 ps.

| Workload | Baseline dynamic | Candidate dynamic | Change | Reduction |
|---|---:|---:|---:|---:|
| Idle | 12.856853 mW | 9.602031 mW | −3.254822 mW | 25.316% |
| Typical | 18.474695 mW | 15.214226 mW | −3.260469 mW | 17.648% |
| Peak | 23.911121 mW | 20.658752 mW | −3.252369 mW | 13.602% |

The paired Icarus gate-level testbench compared `pc`, `memread`, `memwrite`, `aluout`, `writedata`, and `suspend` on every cycle: 200 idle cycles and 300 typical and peak cycles passed with no mismatch or unknown outputs. Both sides had zero unannotated pins for all three workloads. The candidate has 0 DRCs, 0 antenna net/pin violations after diode repair, VDD/VSS connectivity passes with PSM-0069 count 0, and the shared hierarchical Yosys adapter proves 1,123/1,123 points.

The cold optimizer committed two Stage 1 moves (`xrow1004` on `dp.rf.rf[13][1]` and `xrow2005` on `dp.rf.rf[26][15]`). Stage 2 replaced 111 clock buffers and removed residual clock dummy loads. The timing-floor Stage 3 replay used a 0 ps setup-loss budget, retained 40 downsized cells over 42 size steps, and rejected the critical `_3873_` `a21o_2` → `a21o_1` resize. Stage 4 inserted three antenna diodes, rerouted, and finished with zero DRC and antenna violations.

The baseline setup WNS is +15.230 ps and candidate setup WNS is +11.674 ps, a −3.556 ps change. Hold slack increases by +14.051 ps. The candidate now meets both the nonnegative absolute setup requirement and the ≤50 ps setup/hold regression guard.

The latest three-design table and machine-readable evidence are in `build/power/cl_ovps_universal_regression_3bench_7ns_timingclosed_final_20261103/`. The timing-floor RISC-V measurement, route, and paired simulations are under `build/power/cl_ovps_riscv32i_7ns_20261103/timing_floor/`; `final_7ns.*` aliases point to this timing-closed routed result.

## Topology-safe clock-engine upgrade

The Stage 2 clock editor now builds per-endpoint source/parity signatures from the ODB clock graph. It treats every input pin on a hard block macro as a conservative graph boundary, even when its clock-pin name is nonstandard. It accepts only buffer-parent stars whose intermediate net has no direct sink or port tap and whose other loads are pure child buffers or unused dummy inputs. It scores replacement candidates by trial clock power, chooses disjoint stars, replaces each parent and all children atomically, and restores the original cells if endpoint source/parity, timing, clock-gating checks, max slew, or placement checks fail. Dummy pruning is graph-checked and skips any clock domain containing a hard macro input. Stage 1 relocation inventory is restricted to placed standard-cell flops and carries clock-domain/nearest-ICG group tags. Clock-cell scoring includes ICG internal power without misclassifying datapath inverters.

The updated engine passed live Stage 2 graph/preflight runs on the raw pre-restructuring CTS checkpoints:

| Checkpoint | Clock endpoints | Dummies removed | Accepted stars / buffers | Graph, timing, slew, PDN |
|---|---:|---:|---:|---|
| GCD cold run | 35 | 3 | 1 / 5 | PASS |
| AES raw CTS checkpoint | 562 | 96 | 3 / 33 | PASS; 5 timing-unsafe stars rejected |
| RISCV32I raw CTS checkpoint | 1,056 | 92 | 4 / 60 | PASS; 4 timing-unsafe stars rejected |

The synthetic hybrid topology test directly taps the root buffer net with a flop and inserts a lower parent-to-child branch. Stage 2 rejects the tapped root, accepts the untapped lower star, preserves all 35 endpoint source/parity signatures, and passes the timing/slew/PDN preflight. The Yosys torture test also proves a synthetic netlist containing an ICG, a negedge flop, and a mux-selected clock source. Seven tests pass, including data-buffer exclusion, macro-boundary handling, fail-closed source checks, and live OpenROAD hybrid integration. Graph-aware LEC passes on the saved GCD, AES, and RISCV32I final netlists with 52, 562, and 1,123 proof-point results respectively. The earlier AES/RISCV smoke run that counted data-buffer pairs as clock stars exposed a graph-filter bug; the final implementation requires every candidate buffer to be on a traced clock endpoint path. New raw-CTS runs now exercise actual AES/RISCV transformations, and timing-unsafe stars remain unchanged.

GCD also completed a fresh topology-safe cold run from raw `3_place.odb` through detailed route and measured gate-level VCD/SPEF power. The optimizer selected `xrow1006`; the new Stage 2 pruned three dummies and atomically replaced one five-buffer star. It passed route/antenna, VDD/VSS, LEC, zero-unannotated-pin, setup-floor, and 50 ps timing gates:

| Workload | Baseline dynamic | Candidate dynamic | Reduction | Reduction % |
|---|---:|---:|---:|---:|
| Idle | 135.633 µW | 111.186 µW | 24.447 µW | 18.02% |
| Typical | 150.769 µW | 126.086 µW | 24.683 µW | 16.37% |
| Peak | 240.041 µW | 213.728 µW | 26.314 µW | 10.96% |

The fresh GCD result improved routed setup slack by 216.30 ps and changed hold by −3.95 ps. AES and RISCV32I were freshly routed and measured after applying the corrected graph filter. AES requires both `SETUP_SLACK_MARGIN=0.5` and its design-specific `aes_leaf_clock_ndr.tcl`: in a controlled reroute of the same Stage 3 ODB, the generic NDR on 99 nets ended at −18.392 ps, while the AES NDR on 107 nets ended at +1.891 ps. The corrected run passes all measured sign-off gates. Cold raw-CTS Stage 2 runs found and transformed legal stars on AES and RISCV32I; unsafe stars were rejected by timing checks. Support remains Sky130HD-specific; macro and clock-gating scenarios are synthetic topology tests, not live macro/UPF sign-off.

### Fresh AES/RISCV topology-safe regression

All power values below are routed SPEF/VCD dynamic power (internal plus net switching). AES is measured against the same 3.8 ns baseline and uses the corrected graph-filtered CTS checkpoint, its documented 0.5 ns setup-repair margin, and the AES-specific 107-net leaf-clock NDR. Candidate setup WNS is +1.891 ps, improving by 43.839 ps from the −41.947 ps baseline.

| Design / workload | Baseline dynamic | Candidate dynamic | Reduction | Setup WNS baseline → candidate | Other gates |
|---|---:|---:|---:|---:|---|
| AES idle | 16.573 mW | 10.319 mW | 6.253 mW (37.73%) | −41.947 → +1.891 ps (+43.839 ps) | 0 DRC/antenna; PDN PASS; 0 VCD drops; 562/562 LEC |
| AES typical | 75.756 mW | 69.700 mW | 6.056 mW (7.99%) | same | PASS |
| AES peak | 77.147 mW | 71.118 mW | 6.029 mW (7.82%) | same | PASS |
| RISCV32I idle | 12.857 mW | 9.602 mW | 3.255 mW (25.32%) | +15.230 → +11.674 ps (−3.556 ps) | 0 DRC/antenna; PDN PASS; 0 VCD drops; 1,123/1,123 LEC |
| RISCV32I typical | 18.475 mW | 15.214 mW | 3.260 mW (17.65%) | same | PASS |
| RISCV32I peak | 23.911 mW | 20.659 mW | 3.252 mW (13.60%) | same | PASS |

Machine-readable evidence is under `build/power/cl_ovps_topology_upgrade_20261103/`: AES `aes_graph_smoke/measured_signoff_graphfilter_margin50/cl_ovps_manifest.json`, RISCV32I `riscv_graph_smoke/measured_signoff_graphfilter/cl_ovps_manifest.json`, and GCD `gcd_cold/measured_signoff_topology_proof/cl_ovps_manifest.json`. These results validate the corrected implementation on three Sky130HD benchmarks. They do not establish universal support for arbitrary clock mux semantics, generated clocks, hard macros, integrated-gate libraries, or UPF power states. The hybrid tapped-branch and ICG/mux/negedge cases are synthetic tests; unsupported or ambiguous graph roots are rejected conservatively.

The implementation and smoke artifacts are under `scripts/cl_ovps_clock_topology.py`, `scripts/cl_ovps_clock_topology.tcl`, `scripts/make_cl_ovps_hybrid_torture_odb.tcl`, `scripts/test_clock_topology_safety.py`, and `build/power/cl_ovps_topology_upgrade_20261103/`.

## Phase 2 datapath pass evaluation (2026-11-04)

Phase 2 is implemented as opt-in Stage 3 passes and does not alter the Phase 1 results above. The completed ablation, component-power tables, trial outcomes, and reproduction commands are in [CL_OVPS_PHASE2_ABLATION_REPORT.md](CL_OVPS_PHASE2_ABLATION_REPORT.md); the parsed Sky130HD sequential-cell characterization is in [stage3c_liberty_sequential_audit.tsv](stage3c_liberty_sequential_audit.tsv).

Pass 3C audited all 69 sequential master types and all 69 clock/gate pin groups, including alternate `CLK_N` pins, latch `GATE` pins, and integrated clock-gate clock pins. A parser gap that initially recognized only a literal `CLK` was corrected before closeout; the updated audit produced 30 static non-increasing-capacitance replacement pairs and was rerun across GCD, AES, and RISCV32I. It found zero eligible cell instances, and all three corrected pass output ODB hashes match their inputs. Pass 3D built 270 Liberty-derived Boolean functions, found zero GCD mappings, and accepted no fold in the bounded pre-route AES and RISCV32I trials. Both trial output ODBs match their pre-route inputs. As a result, Phase 1 + 3C, Phase 1 + 3D, and the combined no-op ablation carry forward the Phase 1 measured power values. No new Phase 2 routed candidate was created, so no incremental routed saving was established; the identity ablation delta is zero because the implementation checkpoint is unchanged, not because a separate reroute measured zero. The trials do not establish that no complex-gate fold can help on another candidate, benchmark, or timing-closed source checkpoint.

Pass 3E generated routed SDF and arrival-skew diagnostics on all three designs. Its gate arrival skew p50/p95/max is 1.481/4.140/4.290 ns on GCD, 0.336/0.982/2.447 ns on AES, and 1.755/4.672/9.142 ns on RISCV32I. No matched timing-annotated and zero-delay VCD pair was available, so glitch transitions/fraction remain unmeasured; no alignment mutation was applied. Phase 2 is not promoted into default optimization settings: 3C/3D remain opt-in and 3E remains read-only until positive routed savings and all benchmark sign-off gates are demonstrated.

## Phase 3B integrated clock gating update (2026-11-04)

Phase 3B was implemented and evaluated as an opt-in post-placement, pre-CTS pass. It is not a successful power optimization on the tested candidate and is not part of the three-benchmark Phase 1 signoff results above.

The GCD matched ablation started both the ICG and no-ICG flows from the identical Stage 1 ODB. The ICG case inserted one `dlclkp_1` for four sinks, passed route/antenna and VDD/VSS checks, had zero unannotated pins, and passed LEC 52/52. Nevertheless, relative to the same-placement no-ICG control, dynamic power increased by 41.115 µW idle, 37.434 µW typical, and 29.980 µW peak. Setup slack decreased by 352.880 ps, failing the 50 ps regression guard; hold decreased by 27.602 ps. CTS/Stage 2 interaction changed as well: the control ended with zero CTS buffers and five inverters, while the ICG candidate ended with seven buffers and four inverters after timing-unsafe clock-star replacements were rejected.

No ICG was inserted on AES: all five proposed clusters were rejected because the enable cones could not be proven same-clock synchronous. No cluster was formed on RISCV32I under the current minimum-four-sink / maximum-25 µm span policy. Neither design has a Phase 3B routed power result. The GCD clock-gating STA audit passed using a generated STA-only Liberty overlay with a 50% duty-cycle assumption; this is a bounded check and not general ICG or multi-clock signoff.

**Closeout decision:** Keep `--enable-pass-3b` opt-in and do not claim Phase 3B savings. The previously measured GCD/AES/RISCV32I Phase 1 results remain the release baseline. Phase 3B requires a new clock-tree/optimizer interaction and another matched routed ablation before promotion. Detailed counts, power tables, gates, reproduction instructions, and artifact locations are in [CL_OVPS_PHASE3B_ICG_REPORT.md](CL_OVPS_PHASE3B_ICG_REPORT.md).

## Phase 3A ASAP7 multi-Vt evaluation (2026-11-04)

Phase 3A is implemented behind `--enable-pass-3a` / `--enable-multivt` and was exercised through the universal runner's Stage 3 injection. On ASAP7 GCD, the validated same-route trial committed 52 LVT→RVT master swaps. It used a 500 ps SDC, 100 ps instance slack margin, the R/L/SL TT Liberty and LEF variants, matched gate-level idle/typical/peak VCDs, and routed SPEF.

| Workload | Dynamic Δ | Dynamic Δ % | Leakage Δ | Total-power Δ |
|---|---:|---:|---:|---:|
| Idle | −4.800 nW | −0.0033% | −63.551 nW | −68.351 nW |
| Typical | −1.088 µW | −0.420% | −37.661 nW | −1.126 µW |
| Peak | −2.152 µW | −0.526% | −36.832 nW | −2.188 µW |

Setup WNS improved by 0.166 ps (+14.590 → +14.756 ps); hold stayed at +59.702 ps. The candidate had zero unannotated pins, passed candidate-ODB VDD/VSS connectivity, inherited a route whose final DRC and antenna counts are zero, and passed 331/331 Yosys equivalence points. The signal-net and special-net DEF sections are byte-identical across the swap, confirming that the in-place cell changes did not alter routed nets. See [CL_OVPS_PHASE3A_ASAP7_MULTIVT.md](CL_OVPS_PHASE3A_ASAP7_MULTIVT.md) for details and artifacts under `build/power/phase3a_asap7_gcd_500ps/`.

This is a same-route ASAP7 GCD measurement. A separate earlier pre-route experiment followed by fresh CTS/routing increased idle dynamic power, so pre-route acceptance alone is not treated as routed sign-off. This 500 ps benchmark does not add an ASAP7 result to the three-design Sky130HD sign-off table or establish cross-design generality.
