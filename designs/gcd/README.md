# GCD second-design calibration target

This directory adds the OpenROAD Flow Scripts GCD benchmark as an independent
SKY130HD design alongside PicoRV32. The RTL and base timing constraints come
from the workspace-pinned OpenROAD Flow Scripts revision in `versions.lock`.
The clock period is set to 20 ns to match the PicoRV32 calibration target. The
local flow uses a separate design nickname and variant (`gcd_power` /
`gcd_baseline20ns`) so its results do not overwrite the PicoRV32 baseline.

`sim/tb_gcd_activity.sv` generates matched idle, typical, and peak gate-level
VCDs. The non-idle workloads check each response against a testbench GCD
reference before finishing. The single-domain UPF declares the same 1.8 V
SKY130 supply used for PicoRV32; it does not model power gating.

Run the complete baseline flow and matched placement/routed-SPEF reports with:

```sh
./scripts/run_gcd_baseline.sh
```

The baseline report is `build/power/gcd_baseline20ns/power_summary.md`. It
records placement and routed switching power for each workload, pin annotation,
timing slack, and source hash checks. This baseline is the onboarding step for
cross-design calibration; it does not yet establish that a cost model
generalizes.

The placement-stage per-net cost table is checked against OpenSTA's switching
power in `build/power/gcd_baseline20ns/activity_cost_summary.csv`. Since this
ORFS configuration preserves hierarchy, both placement and SPEF capacitance
exporters walk hierarchical nets. The summed placement score matches OpenSTA
within 0.32% for idle, 0.29% for typical, and 0.18% for peak. This validates
the accounting on this design; it does not validate the score's ability to
predict how a changed placement will route.

The routed baseline measured 25.616 µW idle, 32.180 µW typical, and 71.463 µW
peak switching power. All 978 VCD pin activities were annotated in each
workload, setup slack was 12.809 ns, hold slack was 0.571 ns, and all five
placement/routed source hashes matched before and after read-only power
reporting.

For a saved placement candidate, route it in a fresh GCD variant and compare
its own final-netlist VCD/SPEF against the baseline:

```sh
MOVE_CANDIDATE_ODB="$PWD/build/power/gcd_swap_cal_20261027/candidate_odb_relaxed/swap02.odb" \
CANDIDATE_FLOW_VARIANT=gcd_cal_swap02_20261027 \
  ./scripts/run_gcd_candidate_spef_flow.sh
```

The eight-swap calibration is summarized in
`build/power/gcd_swap_cal_20261027/route_analysis/calibration_summary.md`.
Across 24 workload cases, placement deltas matched routed-delta signs in 20
and had 224.3 nW mean absolute error. Seven of the eight candidates met the
routed timing guard, and none improved power in all three workloads. Per-net
attribution found the changes came from wire capacitance; pin capacitance and
activity did not change. The routed clock-net delta ranged from −210.8 nW to
+503.3 nW across the swaps. Two identical-placement reroutes matched the
baseline power and timing exactly, with identical final ODB, netlist, SDC, and
SPEF after removing the SPEF date header. This confirms these route changes
are deterministic in this setup. The cross-design test below shows that the
raw placement estimate carries signal for aggregate GCD power, while
route-specific and clock-network changes still account for sizeable error. The detailed
per-net SPEF attribution is under each `build/power/gcd_cal_swap*_20261027/`
directory.
 
## Cross-design wire-capacitance predictor
 
Run the placement-to-route delta-capacitance experiment with:
```sh
python3 scripts/analyze_cross_design_wirecap_predictor.py
```
 
This exports placed wire capacitance for the baseline and eight saved GCD
layouts, then trains a one-parameter route-ΔC predictor on 12 PicoRV32
layouts. All GCD layouts are held out by design. Seven pass the 50 ps routed
timing guard, giving 21 candidate/workload power comparisons. The source ODB
hashes are checked before and after all 27 OpenROAD exports, and every placed
cap score reconciles to the saved GCD swap audit within 0.0012 nW.
 
On held-out GCD signal nets, raw placed ΔC has 0.085 fF/net MAE versus
0.136 fF/net for zero. The Pico-trained 0.782 scale is worse at 0.093 fF/net,
so the fitted correction does not transfer as well as the direct estimate.
For aggregate signal power, the raw placement score has 81.7 nW MAE versus
250.5 nW for zero and 117.9 nW for the scaled estimate. Including full routed
power and clock nets, raw placement is 216.4 nW MAE versus 377.6 nW for zero.
This small, selected probe set supports further calibration before any learned
cost is added to the placer. The report and row-level predictions are in
`build/power/cross_design_wirecap_20261027/`.

## Expanded activity-contrast calibration

To broaden the GCD check beyond the first nine proposals, the swap generator
now accepts a configurable minimum activity ratio and optional cross-row
matching. The evaluator still requires matching masters and orientations, runs
`check_placement`, checks timing, and restores the original placement after
each trial. With a 2× activity ratio and a 5× output-capacitance limit, the
expanded pool produced 32 proposals. Twenty-five passed placement timing in
all three workloads. Ten were selected across the placement-prediction range
and routed to SPEF; nine remained inside the 50 ps routed timing guard.

Run the saved calibration analysis with:

```sh
python3 scripts/analyze_gcd_expanded_calibration.py \
  --candidate-selection build/power/gcd_swap_cal_expansion_20261027/route_probe_selection.tsv \
  --audit-dir build/power/gcd_swap_cal_expansion_20261027 \
  --route-root build/power --tag exp20261027 \
  --output-dir build/power/gcd_swap_cal_expansion_20261027 --skip-export
```

On the 27 routed timing-safe workload cases, raw placement power predicted
signal-net power with 105.97 nW MAE versus 367.44 nW for a zero-change estimate
and 161.17 nW for the PicoRV32-trained capacitance scale. For full routed
power including clock nets, raw placement MAE was 381.88 nW versus 640.48 nW
for zero-change. Per-net routed wire-capacitance MAE was 0.13961 fF for raw
placement and 0.13896 fF for the Pico scale, compared with 0.21961 fF for
zero-change. These results show useful signal in the direct placement score on
this stratified sample, but do not validate it as a general placer cost.

No timing-safe candidate reduced routed total switching power in all three
workloads. `swap32` reduced idle by 212.6 nW (about 0.83%) and typical by
171.3 nW (about 0.53%), but increased peak by 136.5 nW (about 0.19%). The
largest apparent savings came from `swap24`, which lost 62 ps of setup slack
and failed the 50 ps guard. Therefore no power optimization is accepted yet.
The prediction-stratified candidate list, per-net data, route audits, model
metrics, and input hashes are under
`build/power/gcd_swap_cal_expansion_20261027/`.

## Candidate-held-out placement score

Run the cross-design and layout-held-out predictor comparison with:

```sh
python3 scripts/analyze_cross_design_placement_score.py
```

It uses 12 PicoRV32 candidates and seven older routed-safe GCD layouts for
training, then predicts the nine expanded GCD layouts held out as a group. It
also runs nine GCD leave-one-layout-out folds, keeping all three workloads of
each held-out layout together. Placement criticality and slack are exported
from each GCD placement ODB using the matching 20 ns SDC. Use
`--skip-export` to reuse the hash-checked physical and timing feature exports.

On the 27 held-out candidate/workload cases, the PicoRV32 plus older-GCD
capacitance/geometry ridge predicted routed signal-power deltas with 86.02 nW
MAE, compared with 105.97 nW for direct placed ΔC and 367.44 nW for zero
change. For total routed power including CTS clock changes, the signal-only
ridge estimate had 359.06 nW MAE, compared with 381.88 nW for direct placed ΔC
and 640.48 nW for zero. Its per-net routed-ΔC MAE was 0.13906 fF, only slightly
below the direct estimate at 0.13961 fF. This supports further ranking
experiments but does not yet validate a placer cost.

Placement timing did not improve the GCD LOCO model in this sample. The
timing-augmented signal-power MAE was 90.71 nW versus 83.91 nW for the
capacitance/geometry model; per-net ΔC MAE also rose from 0.14812 fF to
0.15083 fF. The current timing features use constrained instance input-pin
slacks; top-level output-port slacks are not exported in this OpenROAD build.
No learned placement cost has been added.

## Prospective placement-score ranking trial

Run the fresh candidate holdout experiment with:

```sh
python3 scripts/analyze_cross_design_placement_score.py --skip-export
python3 scripts/score_gcd_prospective_candidates.py
python3 scripts/summarize_gcd_prospective_rank.py
```

The trial excluded all 82 cells used in the earlier GCD proposal pools, then
generated six new same-master/orientation swaps. All six passed the placement
legality and 50 ps timing screens. A ridge model trained on 28 earlier routed
layouts ranked three candidates before routing; the remaining three were
same-pool controls. All six were then routed with their own final VCD, ODB,
SDC, and SPEF.

The model's predicted peak-power ranking had Spearman correlation 0.543 with
the six routed results. It placed the best peak candidate in its top three
and the worst candidate last, but its predicted absolute penalties were
smaller than measured. None of the six improved idle, typical, and peak power
together. `swap06` reduced idle by 109.9 nW (0.43%) and typical by 57.7 nW
(0.18%), while increasing peak by 398.8 nW (0.56%); it passed the timing
guard. The best earlier partial result remains `swap32` at −0.83% idle and
−0.53% typical, with +0.19% peak. No candidate is accepted as an
all-workload power improvement.

The full ranking, predicted/per-net scores, frozen selection, routed workload
results, and report are under
`build/power/gcd_prospective_rank_20261027/`. Each route's SPEF report and
integrity record are under `build/power/gcd_prospective_swap*_20261027/`.

Per-net SPEF attribution reconciles to OpenSTA within 0.09%. For `swap06`,
clock-wire changes saved about 110.6 nW in each workload, while non-clock
signal nets added 0.7 nW idle, 52.9 nW typical, and 509.7 nW peak. Peak cost
is concentrated in `net59`, `_016_`, `_119_/_004_`, and `net44`, where positive
wire-capacitance changes are multiplied by high VCD activity. The placement
model's predicted peak signal delta had 211.4 nW MAE and 0.600 rank correlation
against the measured non-clock signal deltas on these six candidates. Clock
cost is not represented in the current score. Detailed summaries, per-net
tables, and candidate comparisons are in
`build/power/gcd_prospective_rank_20261027/attribution/`.

The earlier `UPF-0001` warning was caused by loading the UPF twice when a
candidate ODB already contained `PD_TOP`. The new idempotent loader reuses the
domain, verifies its root element, marks it top-level, and applies 1.8 V. The
baseline and `swap06` placement/SPEF reports and the six-candidate evaluator
now show `POWER_DOMAIN_VERIFIED` without the duplicate-domain warning. Power
totals and per-net attribution are unchanged; the verification logs and
reconciliation are under `build/power/gcd_upf_verification_20261027/`.

## VCD-weighted signal score and clock-cost calibration

Run the separate signal/clock recalibration with:

```sh
python3 scripts/recalibrate_vcd_clock_placement_score.py
```

This fits three otherwise identical cap/geometry models. Each training
candidate receives equal total weight; within a candidate, net rows use
aggregate idle/typical/peak VCD density raised to gamma 0, 0.5, or 1. The six
prospectively routed candidates are excluded from model fitting. On those six,
gamma 1 reduced candidate/workload signal-power MAE from 79.86 nW to 75.87 nW
and worst-workload MAE from 211.36 nW to 200.76 nW. Worst-workload rank
correlation stayed at 0.600, so the activity weighting improved prediction
error modestly but did not improve candidate ordering. All six holdout
candidates increased signal power in every workload, so their 18/18 direction
matches do not show that the model can identify a power improvement. The fitted model is
saved at
`build/power/gcd_activity_clock_recalibration_20261027/activity_gamma_1.json`.
For a fresh ranking pool, score it explicitly with:

```sh
python3 scripts/score_gcd_prospective_candidates.py \
  --model build/power/gcd_activity_clock_recalibration_20261027/activity_gamma_1.json
```

Clock cost remains separate. The pre-CTS global `clk` net has the same fanout,
endpoint count, and HPWL in all six prospective placement ODBs. A ridge proxy
using aggregate signal-placement changes improved clock-delta LOCO MAE
(163.68 nW versus a 217.47 nW training-mean baseline across 16 candidate
folds), but its sign accuracy fell from 12/16 to 11/16. It is not qualified to
credit clock savings; CTS/route results remain required for total-power
claims. The model metrics, predictions, hashes, and full report are under
`build/power/gcd_activity_clock_recalibration_20261027/`.

## Fresh uniform versus VCD-weighted ranking trial

Summarize the frozen shared-pool scores and routed results with:

```sh
python3 scripts/analyze_gcd_activity_model_comparison.py
```

The fresh pool excluded the 94 cells used by earlier proposal pools and
generated eight same-master/orientation swaps. The uniform cap/geometry model
and the gamma 1 VCD-weighted model scored the same frozen placements before
routing. Both produced the identical full order and top three (`swap02`,
`swap08`, `swap01`). On this pool gamma 1 lowered held-out signal-power case
MAE from 66.92 nW to 59.39 nW (11.3%), but did not change signal or total-power
rank correlation or candidate selection. Thus the lower error did not show
additional selection value in this trial.

All eight candidates were routed against their final candidate VCD, ODB, SDC,
and SPEF. All passed the 50 ps setup/hold guard and had 978/978 annotated pins
with zero unannotated pins for every workload. `swap02` was the only candidate
to reduce total power in idle, typical, and peak. Its deltas were −384.4 nW
(−0.283%) idle, −374.2 nW (−0.248%) typical, and −216.2 nW (−0.090%) peak.
The setup and hold slack losses were 8.22 ps and 4.14 ps, respectively, within
the guard.

Per-net SPEF attribution shows that this win came from clock-tree wire
capacitance: the clock subtotal fell by 383.2 nW in every workload, while
non-clock signal power changed by +0.2 nW idle, +10.2 nW typical, and +163.9
nW peak. The current candidate score models signal capacitance and gives no
clock-saving credit. Both models ranked `swap02` first in this pool by
coincidence; gamma 1 did not account for the physical source of the improvement.
Per-net totals reconcile to OpenSTA within 1% for all 24 candidate/workload
rows. Detailed rankings, routed deltas, attribution, and frozen input hashes
are under `build/power/gcd_activity_model_compare_20261027/`.

The clock-cost calibration and its external holdout are documented below.

## CTS-aware clock-cost validation

Run the clock-feature export and held-out analysis with:

```sh
python3 scripts/calibrate_gcd_cts_clock_cost.py
```

The placement-only clock ridge was trained on 16 earlier route-safe candidates
and evaluated on the eight frozen candidates in the uniform-versus-VCD trial.
On the external holdout it had 258.87 nW clock-delta MAE versus 206.69 nW for
the training-mean estimate. Its sign accuracy was 6/8 versus 5/8, but adding
the ridge clock estimate to the VCD signal score worsened worst-total rank
correlation from 0.810 to 0.786 and MAE from 178.82 nW (constant mean clock
term) to 179.66 nW. The placement-only proxy is not qualified.

Direct CTS features also had no variation in this pool: each candidate made
five clock buffers and five clock nets, with 113.15 µm average sink-wire length.
Global-route clock-wire capacitance was unchanged for all eight holdout
candidates and was nonzero for only one of the 16 historical training
candidates. The global-route cap model therefore predicted zero clock delta
for every holdout candidate. Its 149.09 nW MAE is a zero-estimate result with
no rank correlation or sign coverage; combined total-power ranking was
identical to the signal-only ranking. The actual clock-wire differences appear
in detailed-route SPEF, after global-route features are fixed.

The reports, model coefficients, per-candidate clock features, and input
hashes are under `build/power/gcd_cts_clock_cost_20261027/`. Neither clock
estimate is integrated into the placer. The next useful experiment is a
frozen pool that changes the clock-sink coordinate set or creates measurable
clock-route congestion differences. Swapping identical flops only trades
their identities between the same occupied sites, so it would leave the CTS
sink geometry unchanged. Any new candidate family must be timing-screened,
routed, and checked against final SPEF before a clock cost is used in ranking.

## Sequential sink-relocation experiment

The first sink-coordinate experiment is reproducible with these scripts:

```sh
TAG=gcd_sink_trial01  # Choose a new unused tag for each run.
RELOCATION_TAG="$TAG" RELOCATION_CANDIDATE_COUNT=35 \
  bash scripts/run_gcd_flop_relocation_screen.sh
python3 scripts/run_gcd_flop_relocation_routes.py \
  --audit "build/power/gcd_flop_relocation_${TAG}/relocation_audit.tsv" \
  --tag "$TAG" \
  --manifest "build/power/gcd_flop_relocation_${TAG}/route_manifest.tsv" \
  --continue-on-error
python3 scripts/analyze_gcd_flop_relocation_routes.py \
  --experiment-dir "build/power/gcd_flop_relocation_${TAG}"
```

The completed frozen pool is `build/power/gcd_flop_relocation_20261027b/`.
The screen considered all 35 sequential cells; nine legal, timing-safe
relocations were routed. All nine met the 50 ps routed setup/hold guard. One,
`reloc35`, reduced routed switching power in idle, typical, and peak. It moves
`dpath.b_reg.out[13]` from (41,860, 70,720) DBU to (67,620, 70,720) DBU, or
25.76 µm horizontally.

`reloc35` reduced routed switching power by 479.9 nW idle (1.874%), 482.7 nW
typical (1.500%), and 502.5 nW peak (0.703%). The SPEF attribution assigns
480.2 nW of that reduction to clock nets; signal-net power changed by +0.3 nW,
−2.5 nW, and −22.3 nW, respectively. Setup slack fell 11.17 ps and hold
slack fell 4.08 ps, both within the guard. All 978 VCD pin activities were
annotated for every workload.

The placement-stage switching estimate for `reloc35` was +0.13 nW idle,
−14.86 nW typical, and −131.75 nW peak. It missed the idle win and
underpredicted routed savings because the 480 nW clock improvement appeared
after CTS and detailed routing.

CTS still created five buffers and five clock nets for every candidate, while
average sink-wire length varied from 110.13 to 114.34 µm. Global-route clock
wire-cap deltas ranged from −2.13 to +4.76 fF. A nine-candidate leave-one-out
scale from global-route clock capacitance reduced clock-cap prediction MAE
only slightly (1.671 fF versus 1.728 fF for a zero-delta estimate); its
within-pool rank correlation was 0.783 and it got 5/7 non-negligible signs
right. For `reloc35`, it predicted −0.235 fF while final SPEF measured
−2.964 fF. Keep final detailed-route SPEF as the acceptance metric and do not
integrate this GRT estimate into the placer yet.

The detailed report, per-workload clock/signal attribution, GRT features,
route manifests, and hashes are in
`build/power/gcd_flop_relocation_20261027b/analysis/`.

## Randomized local relocation and internal-dynamic holdout

Recreate a fresh seeded run with:

```sh
TAG=gcd_random_local_trial02  # Use a new tag for each run.
RELOCATION_TAG="$TAG" RELOCATION_CANDIDATE_COUNT=35 \
  RELOCATION_RANDOM_SEED=20261030 bash scripts/run_gcd_flop_relocation_screen.sh
python3 scripts/run_gcd_flop_relocation_routes.py \
  --audit "build/power/gcd_flop_relocation_${TAG}/relocation_audit.tsv" \
  --tag "$TAG" \
  --manifest "build/power/gcd_flop_relocation_${TAG}/route_manifest.tsv" \
  --continue-on-error
python3 scripts/analyze_gcd_random_flop_relocation_ranking.py \
  --placement-audit "build/power/gcd_flop_relocation_${TAG}/relocation_audit.tsv" \
  --pool "build/power/gcd_flop_relocation_${TAG}/candidate_pool.tsv" \
  --occupancy-inventory "build/power/gcd_flop_relocation_${TAG}/occupancy_inventory.tsv" \
  --route-manifest "build/power/gcd_flop_relocation_${TAG}/route_manifest.tsv" \
  --routes-root build/power \
  --output-dir "build/power/gcd_flop_relocation_${TAG}/analysis"
```

The next experiment used a seeded pool of 35 same-row moves, each 4–20 µm,
with exact target sites checked against all placed and fixed instances. The
local vacancy limit meant the 35 moves covered 14 distinct sequential sinks.
All 35 passed the placement timing screen, all 35 routed within the 50 ps
setup/hold guard, and every routed workload annotated all 978 VCD pins. The
frozen baseline placement hash remained unchanged.

For this phase, the optimization target is internal plus net-switching power;
leakage is reported separately and excluded. Eight candidates lowered that
dynamic-power sum in idle, typical, and peak. The strongest, `rand023`, moves
`dpath.a_reg.out[8]` from (59,340, 38,080) DBU to (66,240, 38,080) DBU. Its
routed reductions were 3.714 µW idle (2.738%), 3.707 µW typical (2.459%), and
3.565 µW peak (1.485%). Leakage changed by only about 0.003 nW.

The win is mostly internal power in the CTS clock group: −3.649 µW in each
workload. Clock-net switching fell by 58.5 nW, while signal-net switching
changed by +0.2 nW idle, +5.7 nW typical, and +138.9 nW peak. Thus the old
net-switching-only gate rejected this move at peak, but the new
internal-plus-net dynamic gate accepts it in all three workloads.

This candidate did not remove clock buffers or make the tree shallower: both
baseline and candidate had five clock buffers, five clock nets, and depth two.
The fanout distribution changed from `8:2, 9:1, 10:1` to `7:1, 8:1, 10:2`,
and average sink-wire length fell from 113.15 µm to 110.75 µm. A repeat route
reproduced the same power summary, CTS signature, and input hashes. Follow-up
cell-level attribution below identifies a CTS dummy-load master change as the
main source of the internal-power reduction.

The pre-route geometry features are not yet strong enough to rank this
reliably. Clock-sink MST delta had Spearman ρ=0.362 and Kendall τ-b=0.279
against routed clock-group internal power across this pool; HPWL and global
dispersion were weaker. The placement dynamic estimate ranked typical and
peak moderately (ρ=0.697 and 0.660), but idle weakly (ρ=0.194). Because only 14
distinct sinks appear in the 35 moves and one candidate changed the observed
fanout signature, these are exploratory results, not a validated placer cost.

The screen, routes, dynamic-power summaries, rank metrics, and repeatability
record are under `build/power/gcd_flop_relocation_random_local_20261029b/`.
The next experiment should target the pre-CTS geometry conditions that change
CTS fanout assignment, then check that a frozen, more diverse pool can rank
clock-group internal power before integrating a clock-aware objective into
the placer.

### CTS centroid proxy and cell-level attribution

Analyze the frozen candidate pool with:

```sh
python3 scripts/analyze_gcd_cts_branch_proxy.py
```

This extracts actual baseline CTS leaf groups, register clock-pin coordinates
and capacitances, candidate sink locations, per-instance CTS cell power, and
routed clock-group power. Reports are in
`build/power/gcd_flop_relocation_random_local_20261029b/analysis/branch_proxy/`.

The baseline-centroid nearest-branch proxy did not reproduce TritonCTS
assignment in this pool. Its nearest-centroid branch changed for 0/35
candidates, even though `rand023` changed actual CTS membership: its moved flop
left one branch, and CTS membership churn involved two sinks total. The actual
sink-clock-pin load variance increased by 3.422 fF² for `rand023` while
clock-group internal power fell by 3.649 µW. Centroid crossing and lower
cluster-load variance are therefore not supported explanations for this win.

Per-cell power identifies the dominant mechanism. CTS changed balancing dummy
load `clkload2` from `sky130_fd_sc_hd__clkbuf_8` to
`sky130_fd_sc_hd__clkinv_2`. Across the three dummy cells, internal power fell
by 3.658 µW; the five real CTS buffers changed by only +8.64 nW. Their sum
matches the measured clock-group internal-power delta (−3.649 µW) within
rounding. TritonCTS inserts these dummy loads to balance clock-buffer output
capacitance, and they remain real cells in the routed netlist.

The source explains the transition more precisely than a paired-left/right
load model. For each parent net, TritonCTS compares the output capacitances of
its child clock buffers. Each child's output capacitance is the sum of its
direct sink input-pin capacitances; the source explicitly leaves wire
capacitance out of this dummy-load calculation. The largest sibling output
capacitance is the target. For a lower-load child, the deficit is
`Cmax - Cchild`, and CTS picks the eligible dummy cell minimizing
`abs(Cin(dummy) - deficit)`.

For `rand023`, branch 3's direct sink-pin load fell from 14.728 fF to
12.887 fF while the maximum sibling stayed at 18.482 fF. Its dummy deficit
therefore rose from 3.754 fF to 5.595 fF. Liberty input caps are 4.157 fF for
`clkbuf_8` and 5.516 fF for `clkinv_2`; their midpoint is 4.837 fF. The deficit
moved across that midpoint, matching the observed master substitution. This is
not a lower-input-capacitance substitution: `clkinv_2` has the larger input
capacitance, but much lower measured internal power for this workload.

The generated `sibling_dummy_deficits.tsv` checks this rule on the full pool,
and `dummy_cell_input_caps.tsv` records the Liberty caps. This is still a
post-CTS diagnostic because it uses actual CTS branch membership. A useful
placement-only score must first predict the cap-aware sink assignment and
topology, then apply the sibling deficit and discrete dummy-cell rule. The
diagnostic table must not be treated as an independent placement predictor.

The baseline-centroid distance delta has Spearman ρ=0.377 and Kendall τ-b=0.304
against routed clock-group internal power across the 35 candidates. This is
exploratory, and the proxy did not identify the discrete branch reassignment.
TritonCTS uses cap-aware sink matching and H-tree partitioning; its
implementation is in `tools/openroad/src/cts/src/SinkClustering.cpp`,
`HTreeBuilder.cpp`, and `TritonCTS.cpp`. The next model should approximate the
actual sink-clustering and H-tree assignment before applying the source-based
sibling-cap/dummy-cell rule, then be frozen and evaluated on a new candidate
pool while preserving isolated one-flop moves and the 50 ps timing guard.

### CTS-only placement oracle

The fast oracle runs the normal ORFS `do-cts` stage from each saved placement
ODB, without global or detailed routing:

```sh
python3 scripts/run_gcd_cts_oracle.py \
  --pool-dir build/power/gcd_flop_relocation_random_local_20261029b \
  --tag verify20261029 \
  --manifest build/power/gcd_flop_relocation_random_local_20261029b/analysis/cts_oracle_manifest.tsv
python3 scripts/analyze_gcd_cts_oracle.py
```

On the 35-candidate diagnostic pool, ORFS reproduced the routed CTS sink-to-
leaf assignments, CTS cell masters, and dummy-cell branch connections in
36/36 cases, including the baseline. The CTS engine took about 0.35 seconds per
case; the complete ORFS invocation averaged about 1.2 seconds per case.
Placement-parasitic power reports also predicted the `rand023` routed clock
internal-power reduction closely: −3.651 µW predicted versus −3.649 µW routed.

The full-pool rank result is weaker than this one large event: placement-CTS
internal-plus-switching power had Spearman ρ=0.186 and Kendall τ-b=0.135 against
routed clock dynamic power. Clock switching estimates are the weakest part of
the model, as placement parasitics do not capture routed clock-wire
capacitance. The oracle therefore gives an exact early prediction of CTS
topology and dummy master choices in this design, but does not yet rank small
power changes reliably. Its current activity trace comes from a baseline or
candidate workload VCD; the baseline-VCD mode is suitable for a pre-route
evaluation only when that workload trace is available independently of the
candidate route.

The first oracle outputs are under
`build/power/gcd_flop_relocation_random_local_20261029b/analysis/cts_oracle/`.
This old pool was used to validate oracle plumbing, so the next evaluation must
use a newly frozen one-flop relocation pool and compare the fixed oracle score
against routed SPEF power without retuning.

### Frozen CTS-oracle holdout

The next pool used seed 20261030, 35 isolated legal moves, and 14 unique sinks.
All candidates passed placement timing screening. Before routing, the score
was frozen as the delta in CTS-buffer and dummy-load internal plus switching
power from the standard ORFS `do-cts` stage, using placement parasitics and the
same baseline workload VCD for every candidate. Source ODB, CTS ODB, pool,
screen-audit, and oracle-manifest hashes were saved before route.

The CTS-only oracle reproduced the final routed sink-to-leaf assignments,
clock-cell master set, and dummy-cell branch connections in 36/36 cases
(baseline plus 35 moves). The complete CTS oracle run took about 43 seconds;
each CTS engine run took about 0.35 seconds. All 35 candidate routes completed,
passed the 50 ps timing guard, and had complete VCD annotation. Twelve lowered
internal plus net-switching power in idle, typical, and peak workloads.

The frozen clock-dynamic score had Spearman ρ=0.334 and Kendall τ-b=0.224
against routed clock dynamic power in all three workloads. It found the same
three candidates in the routed top three, and four of the routed top five
clock-power candidates. It missed the exact top candidate: `rand022` was the
lowest predicted delta, while `rand008` was the best routed candidate. `rand008`
reduced routed internal plus net-switching power by 4.093 µW idle, 4.094 µW
typical, and 4.035 µW peak, with setup/hold losses of 21.915 ps and 4.136 ps.

This supports CTS-only as a fast **top-candidate filter** for the discrete
dummy-load event. It does not yet provide a strong full-pool ranking: rank
correlation is modest, especially for small changes, and placement estimates
of clock-wire switching remain weak. Keep the dummy-cell internal-power term
separate from routed clock-wire switching and improve the latter's capacitance
proxy before using the score as a placer objective.

The frozen scores and hashes are under
`build/power/gcd_flop_relocation_random_local_20261030a/analysis/frozen_oracle_score/`.
The holdout comparison is under
`build/power/gcd_flop_relocation_random_local_20261030a/analysis/holdout_comparison/`;
the exact topology and placement-power checks are under
`build/power/gcd_flop_relocation_random_local_20261030a/analysis/cts_oracle/`.
