# CL-OVPS Universal Runner

This package provides one design-neutral command interface for running or auditing CL-OVPS on the tested flat mapped Sky130HD flows. Design and flow details come from the supplied ODB, SDC, VCD, Liberty, RC, UPF, and ORFS configuration. The clock-cell restructuring and master mapping are Sky130HD-specific, and unsupported or ambiguous clock paths fail closed; the package is not yet validated as a universal engine for arbitrary PDKs, generated clocks, hard macros, or UPF modes.

## Entry points

- `scripts/run_cl_ovps_universal.py`: top-level CLI. It can run a configured four-stage plan, then reopens the final routed ODB/SPEF and measures idle, typical, and peak activity. Without `--flow_plan`, it is a measurement/signoff audit of supplied artifacts and does not optimize or route them.
- `scripts/run_cl_ovps_stage1.py`: placement search. Screens legal flop relocations, runs a trial CTS for each surviving move, scores total CTS clock-cell internal power, and commits eligible moves under HPWL and timing guards.
- `scripts/run_cl_ovps_cts.py`: ORFS CTS checkpoint for a supplied placement.
- `scripts/run_cl_ovps_pass.py --stage clock`: Stage 2 clock restructuring and dummy pruning. Clock edits use ODB clock-graph endpoint/root/parity signatures, legal parent/child stars, atomic replacement with rollback, timing/slew/clock-gating checks, and fail-closed endpoint verification.
- `scripts/run_cl_ovps_pass.py --stage sizing`: Stage 3 activity-aware cell sizing and safe commutative pin permutation.
- `scripts/run_cl_ovps_stage4.py`: ORFS global/detail route and finish, DRC/antenna and VDD/VSS checks, optional LEC command-plan execution, and recording of the effective timing-repair margin. Without an LEC plan it reports `INCOMPLETE`, even when physical checks pass.
- `scripts/run_cl_ovps_gate_lec.py`: generic Sky130HD LEC adapter for flat mapped cell netlists, including flattened outputs from hierarchical RTL designs. Clock endpoints must be visible in the selected top module; nested hierarchical clock paths fail closed. It analyzes each clock source and endpoint, requires exact source/parity agreement, abstracts only verified pure clock-transport cones, and retains clock muxes, ICGs, macros, and datapath logic in the proof. It ties omitted physical supplies and uses `async2sync` only for designs containing the modeled asynchronous-reset DFF. That reset abstraction samples reset at the clock for the proof.
- `scripts/cl_ovps_clock_topology.py` and `scripts/cl_ovps_clock_topology.tcl`: Python reference and live OpenROAD graph checks. Unknown or ambiguous clock-bearing cells fail closed; clock muxes, gates, macros, sequential clock pins, and primary outputs terminate a candidate star.
- `scripts/test_clock_topology_safety.py`: synthetic topology tests for hybrid tapped branches, ICGs, negedge flops, mux roots, source/parity equivalence, Yosys LEC, and (when the local GCD CTS artifacts are present) an OpenROAD Stage 2 integration test.
- `scripts/cl_ovps_sky130_lec_models.py`: shared Sky130HD functional-model and primitive adapters used by the GCD and generic LEC runners.
- `configs/cl_ovps_flow_plan.global_default.template.json`: four-stage cold flow-plan starter with no activity VCD or simulator command; its power scores are estimates, not signoff measurements.
- `configs/cl_ovps_lec_plan.template.json`: reusable LEC command-plan starter for the generic single-clock adapter.
- `scripts/write_cl_ovps_regression_manifest.py`: combines per-design manifests into one Markdown and JSON report.

The `scripts/audit_gcd_multistage_transform.tcl` path remains as a backward-compatible wrapper. New flows use `scripts/cl_ovps_sky130hd_stage23.tcl` through the Stage 2/3 wrappers.

## Measurement-only / existing-result audit

Required inputs are the candidate final routed ODB, SDC, Liberty, and output directory. Measured workload power uses one gate-level VCD per workload. `--activity_policy global-default` can instead produce an explicitly estimated comparison when VCDs are unavailable. Add baseline artifacts and route/PDN/LEC logs to enable comparative signoff gates:

```sh
python3 scripts/run_cl_ovps_universal.py \
  --design <label> \
  --placed_odb <candidate-6_final.odb> \
  --sdc <candidate-6_final.sdc> \
  --spef <candidate-6_final.spef> \
  --vcd_dir <candidate-vcd-directory> \
  --lib <path-to-liberty> \
  --rc_tcl <path-to-setRC.tcl> \
  --upf <path-to-design.upf> \
  --baseline_odb <baseline-6_final.odb> \
  --baseline_sdc <baseline-6_final.sdc> \
  --baseline_spef <baseline-6_final.spef> \
  --baseline_vcd_dir <baseline-vcd-directory> \
  --route_log <5_2_route.log> \
  --psm_log <power-grid-report.log> \
  --lec_status <LEC-status.txt> \
  --lec_log <LEC-proof.log> \
  --require_signoff \
  --output_dir <new-output-directory>
```

Repeat `--lib` for each Liberty file. If scope inference chooses the wrong VCD hierarchy, pass `--vcd_scope` and/or `--baseline_vcd_scope`. In measured-VCD mode, the runner requires complete annotation for every workload. Global-default mode is for screening only and sets `signoff_eligible` to false. Signoff passes only when measured candidate and baseline VCDs are complete, the candidate improves dynamic power in all supplied workloads, timing degradation stays within the configured guard, and DRC/antenna, VDD/VSS, and LEC evidence pass.

`--upf` is optional. When supplied, the profiler loads its power domains only if the ODB does not already contain them. For a full flow plan, add `--upf <path>` to the Stage 1 and Stage 2/3 commands when those checkpoints need the UPF loaded, and pass `--upf <path>` to the top-level command for baseline/candidate profiling.

## Full four-stage execution

The six required CLI flags are intentionally retained. A real optimization also needs ORFS flow configuration, a work directory, platform settings, and an LEC recipe. Put those project-specific inputs in a JSON plan and pass it with `--flow_plan`. Use `configs/cl_ovps_flow_plan.template.json` for measured-VCD optimization or `configs/cl_ovps_flow_plan.global_default.template.json` for VCD-less screening, then replace the design and flow paths. The latter uses `--activity-policy global-default` in Stage 1 and `--default-activity` in Stages 2/3. The top-level runner must also use `--activity_policy global-default` and records estimated power with `signoff_eligible=false`.

A flow plan has schema version 1 and exactly four ordered stages named `stage1`, `stage2`, `stage3`, and `stage4`. Each stage contains a `command` or `commands` array of argv arrays (commands run without a shell), optional declared `outputs`, and may consume outputs from earlier stages. For example, a stage 2 command can use `{stage1_optimized_odb}`. Each command's output is captured in the top-level output directory and hashed. For measured signoff, the plan's `final` object must resolve to the final routed ODB, SDC, SPEF, and candidate VCD directory. For estimated screening, VCD output may be omitted when using global-default activity. The `evidence` object can point to route, PSM, and LEC artifacts. Set `setup_margin_ns` from the benchmark's validated routing recipe; Stage 4 records both the requested and effective `repair_timing -setup_margin` values. For the validated AES recipe, set `setup_margin_ns` to `0.5` and `pre_global_route_tcl` to `{root}/designs/aes/flow/aes_leaf_clock_ndr.tcl`. The design-specific NDR is needed alongside the margin to reproduce the positive setup slack.

Call the top-level runner with the original placed ODB/SDC and optional activity VCD directory for the optimization inputs, plus the baseline final ODB/SDC/SPEF and (for measured signoff) baseline VCDs. The flow plan then supplies the final candidate files that the runner measures. For measured signoff, generate gate-level VCDs for the candidate implementation and include exactly one `idle`, `typical`, and `peak` VCD below its final VCD directory. Global-default activity can run without a simulator, but its power values are estimates and cannot pass measured workload signoff.

The plan runner does not invent a simulator, testbench, or UPF. The LEC adapter checks graph source and phase per endpoint and retains ICG/mux/macro logic, but each new cell family and clocking construct still needs functional models and design-specific validation. For unsupported semantics, provide a design-appropriate proof command. Do not report measured workload signoff unless all four stage commands actually ran and the final evidence gates passed.

## Reproducible GCD/AES regression

The frozen routed checkpoints were reloaded in OpenROAD and measured through the same top-level CLI. The committed artifacts and unified report are under `build/power/cl_ovps_universal_regression/`. This historical regression remeasured saved winners; it did not regenerate their placement moves.

| Design | Idle reduction | Typical reduction | Peak reduction | Annotation | Route / PDN / LEC |
|---|---:|---:|---:|---:|---|
| GCD | 24.187 µW (17.83%) | 24.438 µW (16.21%) | 26.333 µW (10.97%) | 0 missing | PASS |
| AES | 6.253 mW (37.73%) | 6.056 mW (7.99%) | 6.029 mW (7.82%) | 0 missing | PASS |

The unified report records individual timing deltas and per-gate evidence. Separately, the generic Stage 1, Stage 2, Stage 3, and Stage 4 entrypoints were smoke-tested on the available Sky130HD flows. Those smoke tests establish CLI/flow integration, not a fresh full optimization for each design.

The AES candidate `aes_power38_margin50_20261103a_xrow39070_stage2pathresize_xor3_2` was also audited directly through the top-level signoff CLI using its saved routed artifacts. The audit passed all gates: 0 unannotated pins, 0 DRC/antenna violations, VDD/VSS pass with PSM-0069=0, and LEC 562/562. Candidate setup slack is +1.89 ps, with +43.84 ps setup improvement and −1.17 ps hold change against the baseline; this passes the 50 ps regression guard, with little remaining setup margin. The audit manifest is `build/power/aes_xor3_2_signoff_audit_20261103/cl_ovps_manifest.json`.

## Cold generative validation

Unlike the historical checkpoint regression above, the GCD cold replay started from the raw placed ODB and executed Stage 1 through Stage 4 using the top-level flow-plan runner. Stages 1–3 used global-default activity; after routing, the resulting netlist was gate-simulated for idle, typical, and peak, then final routed SPEF/VCD power was measured against the baseline. That final measured-VCD audit passed all gates:

| Workload | Baseline dynamic | Candidate dynamic | Reduction | Reduction % |
|---|---:|---:|---:|---:|
| Idle | 135.633 µW | 110.950 µW | 24.683 µW | 18.20% |
| Typical | 150.769 µW | 125.909 µW | 24.860 µW | 16.49% |
| Peak | 240.041 µW | 213.833 µW | 26.208 µW | 10.92% |

The run recorded a +192.55 ps setup delta and −3.15 ps hold delta, both within the 50 ps regression guard; 0 unannotated pins; 0 route DRC and antenna violations; VDD/VSS pass with PSM-0069 count 0; and Yosys LEC pass. The flow-plan power estimate made before gate simulation is kept separately and is not treated as the measured result.

A third-design cold validation used the available Sky130HD `riscv32i` ORFS target because this checkout did not provide a directly usable `uart` configuration and Ibex synthesis required an unavailable frontend. Stage 1 selected one legal move (`xrow1007`, +0.039% signal HPWL, −63.596 µW Stage 1 clock-cell score). A Stage 2 level-selection parsing defect was found and fixed during this run; the corrected run replaced 109 clock buffers on parity-safe levels 2 and 3, removed the remaining dummy loads, and checked polarity at 1,056 sinks. Stage 4 passed with 0 DRC, 0 antenna violations, VDD/VSS pass, and LEC pass (1,123 proven, 0 unproven).

The `riscv32i` power comparison used global-default transition profiles, not workload VCDs, and is explicitly not workload signoff evidence: estimated idle reduction was 3.457 mW (23.01%), typical 3.484 mW (18.47%), and peak 3.544 mW (12.93%). At the original 6.0 ns target, baseline and candidate setup WNS were about −632.03 ps and −606.54 ps; the candidate improved setup by 25.49 ps and hold by 31.28 ps. A separate 7.0 ns STA re-evaluation of the routed baseline and candidate gave +168.0 ps and +193.5 ps setup WNS, respectively. This confirms that the existing routed layouts meet the 7.0 ns timing constraint in STA, although the physical flow was generated at 6.0 ns and no workload-specific power signoff is available. The SDCs, timing reports, and input hashes are recorded in `build/power/cl_ovps_cold_riscv32i_20261103/sta_7ns_sensitivity/sta_manifest.json`.

## Platform and interpretation limits

- Current cell master naming and substitutions are for Sky130HD; adapting to another PDK needs a platform-specific cell map and timing/power validation.
- Tier 2 continuous leaf-wire power prediction is intentionally not used as a placement acceptance gate because prior held-out GCD results showed poor precision. Stage 1's total CTS clock-cell internal-power oracle remains the primary placement objective; HPWL and timing are guardrails.
- Power values compare OpenSTA dynamic power (internal plus switching) using supplied VCD activity and SPEF parasitics. They are not total chip power and do not imply leakage improvement.
- The no-VCD global-default path is an estimated screening mode, not automatic activity propagation and not workload signoff. Measured signoff still needs baseline and candidate gate-level workload VCDs with complete annotation.
- The generic LEC adapter has passed on the flat mapped gate-level GCD, AES, and RISC-V netlists (from designs with hierarchical RTL): 51/51, 562/562, and 1,123/1,123 equivalence points. It namespaces the module graph and independently flattens both proof sides, validates endpoint/root/parity correspondence, abstracts only verified CTS transport, maps supported Sky130HD DFF strengths, and ties omitted physical supply pins. Clock graph analysis requires endpoints visible at the selected top; nested hierarchical clock paths fail closed. The asynchronous-reset DFF path uses Yosys `async2sync`, so reset behavior between clock edges is outside that proof model. A synthetic ICG/negedge/mux proof and a live hybrid-branch Stage 2 test pass. These tests do not establish signoff for every generated-clock, macro, gated-clock, or UPF combination; validate each new cell family and design class before use.
- **RISC-V32I 7.0 ns update:** A fresh baseline and cold candidate were routed with a 7.0 ns clock, 1.20 ns input delay, and 1.15 ns output delay. The timing-floor candidate was re-routed and measured from routed SPEF/VCD. Dynamic power fell 3.254822 mW idle (25.316%), 3.260469 mW typical (17.648%), and 3.252369 mW peak (13.602%). Differential simulation passed 200/300/300 cycles; all six outputs matched with no unknown outputs after register-file initialization. Both sides have zero unannotated pins; DRC/antenna are zero, VDD/VSS PSM passes, and generic hierarchical LEC proves 1,123/1,123 points.
- The RISC-V cold search committed two placement moves; Stage 2 replaced 111 clock buffers and pruned residual dummy loads. The timing-floor Stage 3 replay kept 40 downsized cells across 42 size steps while rejecting the critical `_3873_` `a21o_2` → `a21o_1` resize. Stage 4 inserted three antenna diodes and finished with zero DRC and antenna violations.
- **RISC-V timing closure:** Baseline setup WNS is +15.230 ps and candidate setup WNS is +11.674 ps (−3.556 ps delta); hold slack improves by +14.051 ps. The candidate meets the absolute nonnegative setup floor and the ≤50 ps regression guard.
- The latest per-design manifest is `build/power/cl_ovps_riscv32i_7ns_20261103/timing_floor/measured_vcd_signoff_final/cl_ovps_manifest.json`; the unified three-design report is `build/power/cl_ovps_universal_regression_3bench_7ns_timingclosed_final_20261103/cl_ovps_unified_signoff.md`. This is timing-closed evidence for the tested Sky130HD designs, not proof of support for arbitrary multi-clock, clock-gated, macro, or UPF designs.

## Topology-safe clock restructuring update

Stage 2 traces each sequential and ICG clock endpoint and treats every input pin on a hard block macro as a conservative graph boundary, independent of the macro's clock-pin naming. It records source and inversion parity for those boundaries. A candidate parent output is eligible only when every load is a child pure buffer or an unused balancing-load input, with no direct sink or top-level tap. Disjoint parent/child stars are scored by trial clock power, selected as a maximum-weight disjoint set, and replaced together. Each trial rolls back on source/parity change, setup/hold guard failure, absolute setup-floor failure, clock-gating check failure, or max-slew failure. Dummy pruning uses the same graph and guard checks and conservatively skips any clock domain containing a hard macro input.

Stage 1 relocation inventory now includes the traced clock domain and nearest ICG parent. Only placed Sky130HD register sinks are movable, and proposed moves remain within their domain/ICG group region. CTS power scoring includes ICG internal power while excluding datapath inverters from the clock-cell subtotal. The Stage 4 LEC adapter no longer ties every sequential clock to one global input: it compares per-endpoint source/parity and abstracts only verified pure clock transport, preserving ICGs, muxes, macro logic, and datapath cells.

Validation on 2026-11-03:

| Checkpoint | Clock endpoints | Dummy loads removed | Accepted stars / buffers | Stage 2 graph and preflight |
|---|---:|---:|---:|---|
| GCD CTS smoke | 35 | 3 | 1 / 5 | PASS; VDD/VSS PASS |
| AES raw CTS checkpoint | 562 | 96 | 3 / 33 | PASS; 5 timing-unsafe stars rejected |
| RISCV32I raw CTS checkpoint | 1,056 | 92 | 4 / 60 | PASS; 4 timing-unsafe stars rejected |
| Synthetic tapped-root GCD topology | 35 | 0 | 1 / 2 | Tapped root refused; safe lower star accepted; endpoint signatures PASS |

The synthetic Yosys torture proof passes with an ICG, a negedge flop, and a mux-selected clock source. Seven tests pass, including live OpenROAD hybrid-branch integration when local build artifacts are available, macro-boundary handling, fail-closed roots, and exclusion of buffer-shaped datapath pairs. The initial AES/RISCV smoke exposed a false clock-star match on data buffers; the final implementation now requires each candidate buffer to be on a traced clock endpoint path. The cold raw-CTS runs above show actual transformations on AES/RISCV; the earlier saved-checkpoint no-op runs remain useful as validators for already-restructured trees. Graph-aware LEC retains ICG/mux/data logic and verifies source and parity at every sink.

GCD also completed a fresh cold run from the raw placed ODB through Stage 1, CTS, topology-safe Stage 2, Stage 3, detailed route, graph-aware LEC, and new gate-level VCD/SPEF measurement. The run chose `xrow1006` (−7.004 µW Stage 1 clock-cell estimate, +1.071% signal HPWL), pruned three dummies, atomically replaced one five-buffer star, and passed the 35-endpoint graph checks. Measured results:

| Workload | Baseline dynamic | Candidate dynamic | Reduction | Reduction % |
|---|---:|---:|---:|---:|
| Idle | 135.633 µW | 111.186 µW | 24.447 µW | 18.02% |
| Typical | 150.769 µW | 126.086 µW | 24.683 µW | 16.37% |
| Peak | 240.041 µW | 213.728 µW | 26.314 µW | 10.96% |

All three GCD workloads had zero unannotated pins. Routed setup slack improved by 216.30 ps and hold changed by −3.95 ps. Route/antenna, VDD/VSS, LEC, nonnegative setup-floor, and 50 ps setup/hold gates passed. Fresh topology-safe route/VCD/SPEF measurements also completed on AES and RISCV32I; see the regression table below. These results cover the tested Sky130HD designs and synthetic topology fixtures, not arbitrary PDKs or all generated-clock, ICG, macro, and UPF configurations.

### Fresh AES/RISCV32I graph-filtered route and measured power

AES uses both the validated 0.5 ns setup-repair margin and the design-specific leaf-clock NDR during global routing. A controlled reroute from the same Stage 3 ODB showed that the margin alone is insufficient: the generic NDR on 99 nets yielded −18.392 ps setup WNS, while AES's custom NDR on 107 nets yielded +1.891 ps. Stage 4 now accepts `--pre-global-route-tcl` and records that Tcl and its hash in the manifest. All power values are routed dynamic power from matched baseline/candidate gate-level VCDs and SPEFs.

| Design / workload | Baseline | Candidate | Reduction | Setup WNS baseline → candidate | Sign-off |
|---|---:|---:|---:|---:|---|
| AES idle | 16.573 mW | 10.319 mW | 6.253 mW (37.73%) | −41.947 → +1.891 ps (+43.839 ps) | PASS |
| AES typical | 75.756 mW | 69.700 mW | 6.056 mW (7.99%) | same | PASS |
| AES peak | 77.147 mW | 71.118 mW | 6.029 mW (7.82%) | same | PASS |
| RISCV32I idle | 12.857 mW | 9.602 mW | 3.255 mW (25.32%) | +15.230 → +11.674 ps (−3.556 ps) | PASS |
| RISCV32I typical | 18.475 mW | 15.214 mW | 3.260 mW (17.65%) | same | PASS |
| RISCV32I peak | 23.911 mW | 20.659 mW | 3.252 mW (13.60%) | same | PASS |

Both candidates have zero unannotated VCD pins, zero route DRC/antenna violations, passing VDD/VSS checks, and passing LEC (AES 562/562; RISCV32I 1,123/1,123). AES's baseline remains timing-negative, but the candidate has +1.891 ps setup WNS and passes the relative guard. The new raw-CTS Stage 2 runs accepted 3 AES stars (33 buffers) and 4 RISCV32I stars (60 buffers), pruning 96 and 92 dummy loads respectively; timing-unsafe stars were left unchanged.

## Phase 2 datapath passes (experimental and opt-in)

Phase 2 adds independently selectable Stage 3 options `--enable-pass-3c`, `--enable-pass-3d`, and `--enable-pass-3e` to the universal CLI/Stage 3 wrapper. They are disabled by default, preserve the Phase 1 clock flow, and write separate per-pass logs, ODB checkpoints, and JSON manifests.

- **3C, sequential master sizing:** `scripts/cl_ovps_pass3c_seq_sizing.py` audits Liberty sequential semantics, clock pin capacitance, and operating-point internal power before isolated instance trials. It only tries same-semantic replacements; trial edits are rejected unless clock cap and local/full-design dynamic power improve, timing/hold/slew limits pass, and placement remains legal.
- **3D, complex-gate folding:** `scripts/cl_ovps_pass3d_gate_folding.py` builds functions from Liberty `function` attributes, discovers local single-fanout chains, and checks exact truth-table and pin-map equivalence. Each proposal runs in an isolated OpenROAD trial and requires physical, timing, dynamic-power, and graph-aware LEC checks. Proposals and rejection reasons are retained in `stage3d_trial_report.tsv` and `stage3d_rejections.tsv`. Physical-only cells are removed only from the temporary functional LEC view; the implementation ODB remains intact.
- **3E, arrival/glitch diagnostics:** `scripts/cl_ovps_pass3e_glitch_diag.py` is read-only. It writes routed SDF, per-gate arrival-skew and net-capacitance TSVs, and compares zero-delay versus SDF gate-level VCDs when a matched pair is supplied. The optional alignment experiment stays disabled unless a measured positive glitch-transition delta is available; it does not insert delay buffers.

Invoke 3C/3D through a configured flow plan so each pass can consume the correct Stage 3 checkpoint:

```sh
python3 scripts/run_cl_ovps_universal.py \
  --flow_plan <plan.json> \
  --enable-pass-3c --enable-pass-3d \
  --pass3d-max-trials 64 \
  --output_dir <new-run-directory>
```

Pass 3D requires LEFs; use repeated `--pass3d-lef` arguments when invoking its standalone CLI. The Stage 3 wrapper accepts `--enable-pass-3e`, `--pass3e-spef`, and, for a paired VCD audit, both `--pass3e-zero-delay-vcd` and `--pass3e-sdf-vcd` with `--pass3e-scope`. The top-level post-route diagnostic accepts `--run-3e-diagnostic` and an optional `--sdf_vcd_dir` whose `typical` VCD is paired with the measured candidate typical VCD. The standalone diagnostic accepts `--zero-delay-vcd` and `--sdf-vcd` plus matching scopes. If the pair cannot be matched, the manifest records a pending glitch measurement and no alignment edit is eligible.

### Phase 2 evidence on GCD, AES, and RISCV32I

The corrected Liberty audit covered 69 sequential master types, all 69 clock/gate pins (including `CLK_N`, latch `GATE`, and integrated clock-gate clock pins), and 270 combinational truth functions; 30 sequential replacement pairs passed the static non-increasing clock-capacitance filter. The audit was updated after final review found that a literal-`CLK`-only lookup missed alternate Liberty clock-pin names; Pass 3C was rerun on all three benchmarks with the corrected parser. It found no eligible cell instances and preserved all three input ODB hashes. Pass 3D found no GCD mappings; its bounded pre-route trials accepted no AES or RISCV32I folds. Their candidate pre-route ODB hashes also match the inputs. Consequently the measured ablation rows for `Phase 1 + 3C`, `Phase 1 + 3D`, and the combined pass are identity results carrying forward the Phase 1 measured SPEF/VCD data; there is no incremental Phase 2 power claim.

Pass 3E generated routed arrival diagnostics, but paired SDF/zero-delay VCD glitch measurements remain unavailable. Skew p50/p95/max was GCD 1.481/4.140/4.290 ns, AES 0.336/0.982/2.447 ns, and RISCV32I 1.755/4.672/9.142 ns. Glitch counts are unmeasured and alignment remains disabled. See [the full Phase 2 ablation report](../reports/CL_OVPS_PHASE2_ABLATION_REPORT.md) and [the Liberty sequential audit](../reports/stage3c_liberty_sequential_audit.tsv) for all workload power splits, search counts, manifests, and reproduction commands. Phase 2 is not promoted to the default flow because no positive held-out routed savings were established.

## Phase 3B integrated clock gating evaluation (2026-11-04)

Phase 3B is implemented behind `--enable-pass-3b` for flow-plan runs. The pass extracts mapped synchronous enable groups and eligible direct feedback-mux loops after placement, applies same-clock enable-cone checks and spatial cluster limits, inserts Sky130HD `dlclkp` cells before CTS, then audits the CTS and routed gated-clock paths. The GCD implementation is logically proven and physically routable, but the matched routed ablation **worsened power on all workloads and failed the 50 ps setup-regression guard**. Phase 3B is therefore not enabled by default and does not alter the three benchmark Phase 1 signoff results above.

On GCD, 32 of 35 sequential instances were extracted as candidates; the current policy formed one eligible four-sink cluster and inserted one `dlclkp_1`. The no-ICG control used the exact same Stage 1 optimized ODB. Relative to that control, the ICG flow increased routed dynamic power by 41.115 µW idle, 37.434 µW typical, and 29.980 µW peak. Setup slack regressed 352.880 ps; hold slack regressed 27.602 ps. Both runs had 0 unannotated pins, 0 DRC/antenna errors, passing VDD/VSS checks, and LEC pass (52/52 for the ICG candidate).

AES analysis found 128 candidate enable flops in 14 groups and five spatial clusters, but the insertion safety check rejected every cluster because the enables could not be proven to depend only on same-clock state/constants. RISCV32I produced 1,024 candidates in 65 groups but no cluster met the current minimum-size and 25 µm span policy. These are no-insertion outcomes, not power measurements and not proof that another RTL/synthesis representation cannot expose a safe group.

The post-CTS and routed gate checks use a generated STA-only Liberty overlay because the Sky130HD Liberty model in this flow lacks the direct clock-gating relation required by OpenSTA. The checked gated-clock setup/hold constraints assume 50% duty cycle. Treat this as a bounded timing audit, not general signoff for arbitrary ICG duty cycles, generated clocks, multiple domains, macros, or UPF states.

See [the Phase 3B ICG report](../reports/CL_OVPS_PHASE3B_ICG_REPORT.md) for pass mechanics, exact workload deltas, applicability counts, reproduction command, manifests, and limitations. The implementation is in `scripts/cl_ovps_pass3b_icg_insertion.py`, `scripts/stage3b_icg_extract.tcl`, `scripts/stage3b_icg_insertion.tcl`, and `scripts/cl_ovps_pass3b_postcts_check.py`.

## Phase 3A ASAP7 multi-Vt evaluation (2026-11-04)

Phase 3A adds an opt-in ASAP7 same-footprint threshold-voltage pass to Stage 3. `scripts/cl_ovps_pass3a_multivt.py` audits R/L/SL Liberty and LEF variants, forms families only when Boolean/pin signatures and physical signal-pin geometry match, and exports pin capacitance, internal-power, and leakage data. Its OpenROAD trial uses `swapMaster` in place, profiles per-instance setup slack in SI units, and rolls back candidates that fail setup/hold, slew/capacitance, internal-plus-leakage, or supplied-VCD dynamic-power guards. `run_cl_ovps_universal.py --enable-pass-3a` and alias `--enable-multivt` append the pass after the configured Stage 3 command.

The ASAP7 GCD trial used a 500 ps SDC because the default 310 ps baseline had negative setup slack. The matched same-route candidate accepted 52 LVT→RVT swaps (83 candidates scanned; two trial rejections), using a 100 ps per-instance setup margin. Gate-level VCD/SPEF analysis measured dynamic-power deltas of −4.80 nW idle (−0.0033%), −1.088 µW typical (−0.420%), and −2.152 µW peak (−0.526%). Leakage fell by 63.55 nW idle, 37.66 nW typical, and 36.83 nW peak. Setup WNS improved from +14.590 ps to +14.756 ps; hold remained +59.702 ps. Each VCD covered 1,178 pins with zero unannotated pins, and Yosys proved 331/331 equivalence points.

This is a bounded same-route ASAP7 GCD evaluation, not a cold Stage 1–4 optimization result: swaps were applied to a routed ODB, the signal-net and special-net DEF sections were byte-identical before and after, and the same routed SPEF was used for both power measurements. The route log ends at zero DRC and antenna violations, and a candidate-ODB VDD/VSS connectivity check passed. An earlier broad pre-route trial followed by fresh CTS/routing increased idle dynamic power, so this result does not establish that every pre-route candidate retains its power ranking after rerouting. It demonstrates modest dynamic-power improvement and larger relative leakage reduction on this small block; it does not validate a universal ASAP7 optimizer.

See [CL-OVPS Phase 3A ASAP7 report](../reports/CL_OVPS_PHASE3A_ASAP7_MULTIVT.md) for methodology, reproduction commands, measured breakdown, and limitations. The matched final sign-off manifest is `build/power/phase3a_asap7_gcd_500ps/measured_signoff_final_3a/cl_ovps_manifest.json`.
