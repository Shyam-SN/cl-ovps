# CL-OVPS Phase 3B: Integrated Clock Gating Evaluation

**Evaluation date:** 2026-11-04  
**Platform:** Sky130HD  
**Disposition:** Implemented and evaluated; not promoted to the default optimization flow.

## Summary

Phase 3B adds an opt-in post-placement, pre-CTS pass that finds synchronous enable groups, clusters nearby sequential sinks, inserts `sky130_fd_sc_hd__dlclkp_*`, and asks CTS to build the clock tree through the new gate. The insertion is deliberately conservative: enable cones must be proven to depend only on state in the same clock domain or constants; ambiguous, asynchronous, cross-domain, and spatially underfilled groups are rejected.

The pass ran through route, VCD/SPEF power measurement, and LEC on GCD. A matched no-ICG control used the identical Stage 1 placement ODB. The GCD ICG run passed route, power-grid, and logical-equivalence checks, but **failed the optimization acceptance criteria**: measured dynamic power increased in all three workloads and setup slack regressed by 352.9 ps, exceeding the 50 ps guard.

AES and RISCV32I were analyzed and attempted with the same insertion safeguards. Neither had a safe cluster to insert. Phase 3B therefore produced no measured power result on those two designs.

## What the pass does

1. `stage3b_icg_extract.tcl` inventories mapped enable flops and direct feedback-mux loops in the placed ODB. It records the clock, enable, data, location, and sequential roots for each candidate.
2. `cl_ovps_pass3b_icg_insertion.py` groups candidates by cell pattern, clock, and enable. It forms clusters with at least four sinks, no more than 16 sinks per gate, and at most 25 µm cluster diameter. The selected gate drive is based on sink count.
3. `stage3b_icg_insertion.tcl` inserts the gate before CTS, connects its `CLK` and `GATE`, and reconnects the selected sinks to `GCLK`. For a proven direct feedback-mux pattern, it can remove the mux and connect the new-data input to the flop. Mapped enable flops retain their data-enable function.
4. The flow runner adds pre-CTS insertion, post-CTS clock-gating timing checks, and a final routed check when `--enable-pass-3b` is supplied with a flow plan.
5. The LEC adapter expands the inserted clock gate only in its proof representation and compares the resulting state transition against the original enable-flop behavior. The routed implementation keeps the physical ICG.

The post-CTS and routed gate checks passed for the one GCD ICG. Sky130HD's Liberty view does not provide the needed direct clock-gate timing relation for this OpenSTA setup, so the check uses a generated **STA-only Liberty overlay** that describes the gated-clock relation and applies a 50%-duty-cycle setup/hold check. This is a tool-specific timing abstraction, not proof of arbitrary duty-cycle, generated-clock, or multi-clock ICG behavior.

## Applicability on the three benchmarks

| Design | Sequential instances | Extracted candidates | Candidate groups | Spatial clusters planned | ICGs inserted | Outcome |
|---|---:|---:|---:|---:|---:|---|
| GCD | 35 | 32 | 3 | 1 (4 sinks) | 1 `dlclkp_1` | Routed and measured; power/timing acceptance failed |
| AES, 3.8 ns | 562 | 128 | 14 | 5 | 0 | Every planned cluster failed same-clock synchronous-enable proof |
| RISCV32I, 7.0 ns | 1,056 | 1,024 | 65 | 0 | 0 | No group met the current minimum-size / 25 µm clustering policy; 32 unsupported sequential instances were excluded |

These are results for the current extractor and clustering limits. They do not prove that AES or RISCV32I contain no RTL opportunity for clock gating; they show that this pass did not find a cluster it could safely transform in these placed netlists.

## Matched GCD routed ablation

The control copied the Phase 3B run's Stage 1 optimized ODB. Thus both Stage 2 flows start with the same placement; the ICG insertion and its interaction with CTS/clock optimization are the treatment being measured. Both sides use routed SPEF and paired gate-level VCDs with scope `tb_gcd_activity/dut`.

| Workload | No-ICG control | With ICG | Change (ICG − control) | Change % |
|---|---:|---:|---:|---:|
| Idle | 111.186 µW | 152.301 µW | **+41.115 µW** | **+36.98%** |
| Typical | 126.086 µW | 163.520 µW | **+37.434 µW** | **+29.69%** |
| Peak | 213.728 µW | 243.707 µW | **+29.980 µW** | **+14.03%** |

The inserted GCD cluster used the mapped `enable_flop` pattern, so it did not exercise direct feedback-mux deletion; the data-enable behavior remained in the flops.

Positive changes mean the ICG implementation consumed more dynamic power. The measured power manifest reports zero unannotated pins for every workload on both sides.

| Timing metric | No-ICG control | With ICG | Change |
|---|---:|---:|---:|
| Setup slack | 13.025252 ns | 12.672372 ns | **−352.880 ps** |
| Hold slack | 0.566843 ns | 0.539241 ns | **−27.602 ps** |

The setup regression exceeds the 50 ps guard. Setup slack remains positive in both cases, but the candidate fails the relative timing gate.

### Observed clock-tree interaction

In the matched final reports, the no-ICG control had zero CTS buffers and five inverters. The ICG case had seven CTS buffers and four inverters. Inserting the gate changed CTS's tree and caused the Stage 2 clock optimizer to reject timing-unsafe star replacements that it accepted in the control. The ICG therefore cannot be assessed as an isolated cell-power saving: the resulting CTS topology and the clock-restructuring decisions dominate this run's outcome.

### Verification gates

- **VCD annotation:** pass; 0 unannotated pins on both sides for idle, typical, and peak.
- **Detailed route:** pass; 0 DRC and 0 antenna violations.
- **Power grid:** pass; VDD and VSS connected, PSM-0069 count 0.
- **LEC:** pass; 52 proven points, 0 unproven.
- **Clock-gating STA audit:** pass under the documented STA-only overlay; one setup and one hold check, zero violations.
- **Optimization gates:** fail; power worsened in all workloads and setup regression exceeded 50 ps.
- **Regression tests:** `python3 -m unittest scripts.test_clock_topology_safety` passed 9 tests, including synthetic ICG/mux/negedge and topology-safety cases.

The physical and logical checks establish that this particular transformed netlist routed and matched the proof model. They do not override the failed power/timing acceptance gates or establish broad ICG signoff coverage.

## Reproduction and evidence

Enable the insertion in a flow-plan run with a fresh output directory:

```sh
python3 scripts/run_cl_ovps_universal.py \
  --design gcd_phase3b_cold \
  --placed_odb <gcd-3_place.odb> \
  --sdc <gcd-placement.sdc> \
  --lib tools/openroad-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib \
  --upf designs/gcd/flow/constraints/gcd.upf \
  --flow_plan configs/phase3b/gcd_flow_plan.json \
  --enable-pass-3b \
  --output_dir <fresh-output-directory>
```

For the matched control, use `configs/phase3b/gcd_control_flow_plan.json`, omit `--enable-pass-3b`, and set `--placed_odb` to the ICG run's `stage1/optimized.odb`. The control plan copies that exact ODB, so Stage 2 starts from the same placement. The flow-plan templates contain the CTS, clock pass, sizing, routing, gate-level VCD, and LEC commands. Use fresh output directories because the runner refuses to overwrite existing results.

Key artifacts from the completed runs:

- ICG insertion and candidate extraction: [Phase 3B manifest](../build/power/phase3b_gcd_cold_20261104/stage2/pass3b/phase3b_manifest.json), [feedback candidates](../build/power/phase3b_gcd_cold_20261104/stage2/pass3b/feedback_candidates.tsv), [cluster plan](../build/power/phase3b_gcd_cold_20261104/stage2/pass3b/icg_clusters.tsv), and [insertion decisions](../build/power/phase3b_gcd_cold_20261104/stage2/pass3b/insertion.tsv).
- Post-CTS and final clock-gating audits: [post-CTS audit](../build/power/phase3b_gcd_cold_20261104/stage2/pass3b/postcts_audit/clock_gating_check_manifest.json) and [routed audit](../build/power/phase3b_gcd_cold_20261104/stage4/pass3b_final_audit/clock_gating_check_manifest.json).
- Matched measured comparison and gate outcomes: [power/ablation manifest](../build/power/phase3b_gcd_matched_ablation_20261104/cl_ovps_manifest.json).
- Routed ICG signoff evidence: [ICG Stage 4 manifest](../build/power/phase3b_gcd_cold_20261104/stage4/stage4_manifest.json).
- Matched no-ICG control evidence: [control Stage 4 manifest](../build/power/phase3b_gcd_control_cold_20261104/stage4/stage4_manifest.json).
- AES and RISCV32I insertion decisions: [AES manifest](../build/power/phase3b_apply_aes_20261104/phase3b_manifest.json) and [RISCV32I manifest](../build/power/phase3b_apply_riscv32i_20261104/phase3b_manifest.json).

## Decision

Keep Phase 3B **opt-in and experimental**. Do not include its GCD result in CL-OVPS power-improvement claims. Before reconsidering promotion, the flow needs a clock-tree strategy that preserves or improves the no-ICG CTS/Stage 2 result, then a new matched routed ablation must pass the power, setup/hold, VCD, route, PDN, and LEC gates. AES and RISCV32I need separate safe enable-group opportunities before their Phase 3B power effect can be measured.
