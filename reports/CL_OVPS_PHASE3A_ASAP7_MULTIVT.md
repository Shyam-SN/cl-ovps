# CL-OVPS Phase 3A — ASAP7 multi-Vt in-place swapping

**Status:** Implemented and validated on a matched same-route ASAP7 GCD candidate. This is an experimental, platform-specific result; it does not extend the Sky130HD sign-off table or establish universal ASAP7 support.

## What Phase 3A does

ASAP7 provides RVT, LVT, and SLVT standard-cell libraries with same-sized variants for many logical cells. Phase 3A parses supplied Liberty variants and R/L/SL LEFs, then groups cells only when their logical pin functions, sequential signatures, physical dimensions, and signal-pin geometry agree. The audit records pin capacitance, internal-power tables, and cell/state-dependent leakage in `stage3a_multivt_family_audit.tsv`; candidate adjacent-Vt replacements are listed in `stage3a_multivt_replacement_pairs.tsv`.

The OpenROAD Tcl pass profiles per-instance setup slack, orders same-family replacements, and trials each `swapMaster` in memory. It checks that instance location and placement status do not change. A swap is committed only if setup and hold guards, the absolute nonnegative setup floor for a closed baseline, max-slew/max-cap checks, and the internal-plus-leakage objective pass. When a VCD is supplied, trial dynamic power must also be non-increasing for that workload. The faster-Vt recovery direction is implemented for negative-WNS baselines; the measured GCD run started timing-closed and exercised LVT→RVT only.

OpenSTA pin-property slack is returned in Liberty user units. The pass converts it with `sta::time_ui_sta` before comparing against the configured margin. This ensures the 100 ps per-instance threshold is applied in the same SI units as WNS and the trial guard.

## ASAP7 GCD result

The baseline is the routed ORFS ASAP7 GCD checkpoint at a 500 ps clock period. The initial 310 ps placement checkpoint had −16.927 ps setup WNS, so it was not used as the optimization anchor. The multi-Vt audit found 202 complete three-Vt master families and 808 adjacent directional replacement pairs. The trial accepted 52 LVT→RVT swaps and rejected two candidates whose idle-VCD dynamic power increased. The same-route candidate was evaluated with the baseline routing and extracted SPEF.

| Workload | Baseline dynamic | Candidate dynamic | Dynamic Δ | Dynamic reduction | Leakage Δ | Total-power Δ |
|---|---:|---:|---:|---:|---:|---:|
| Idle | 147.518 µW | 147.513 µW | −4.800 nW | 0.0033% | −63.551 nW | −68.351 nW |
| Typical | 259.408 µW | 258.320 µW | −1.088 µW | 0.4195% | −37.661 nW | −1.126 µW |
| Peak | 408.709 µW | 406.558 µW | −2.152 µW | 0.5264% | −36.832 nW | −2.188 µW |

Baseline and candidate setup WNS were +14.590 ps and +14.756 ps; hold slack was +59.702 ps for both. The design remained above the absolute setup floor and inside the 50 ps timing guard. Each workload VCD annotated 1,178 pins and left zero pins unannotated. Gate-level differential simulation passed 200 idle, 300 typical, and 300 peak cycles. Yosys reported 331 proven equivalence points and zero unproven points.

The candidate ODB passed `check_power_grid` for VDD and VSS. Signal-net and special-net DEF sections were byte-identical across the swap. The inherited routed checkpoint's final detailed-route count is zero DRC; antenna checks report zero net and pin violations. Routing was not rerun after the VT swap: matched cell dimensions and pin geometry preserve the existing route, and the DEF byte comparison confirms that signal and power routes did not move.

## Integration and reproduction

`run_cl_ovps_universal.py --enable-pass-3a` (also `--enable-multivt`) appends Phase 3A after the Stage 3 command in a four-stage flow plan. Pass `--pass3a-lef` once for each R/L/SL cell LEF. Pass `--pass3a-vcd` and `--pass3a-scope` to activate a VCD-driven dynamic-power guard. The standard-cell Liberty set for this experiment contains the 15 TT libraries (AO, INVBUF, OA, SEQ, SIMPLE × LVT, RVT, SLVT), excluding SRAM and fake timing-only libraries.

From the project root, this Bash snippet discovers the 15 relevant TT Liberty files and reruns the Pass 3A trial into a fresh output directory:

```bash
ROOT="$PWD"
WORK="$ROOT/build/power/phase3a_asap7_gcd_500ps"
PLATFORM="$ROOT/tools/openroad-flow-scripts/flow/platforms/asap7"
NLDM="$PLATFORM/lib/NLDM"
libs=()
for family in AO INVBUF OA SEQ SIMPLE; do
  for vt in LVT RVT SLVT; do
    for lib in "$NLDM"/asap7sc7p5t_"$family"_"$vt"_TT_nldm*.lib*; do
      [[ -f "$lib" ]] && libs+=(--lib "$lib")
    done
  done
done

python3 scripts/cl_ovps_pass3a_multivt.py \
  --input-odb "$WORK/results/asap7/gcd/base/6_final.odb" \
  --sdc "$WORK/results/asap7/gcd/base/6_final.sdc" \
  --rc-tcl "$PLATFORM/setRC.tcl" \
  --spef "$WORK/results/asap7/gcd/base/6_final.spef" \
  --vcd "$WORK/pass3a_routed_idle_trial_15lib/activity_vcd/combined/idle.vcd" \
  --scope tb/dut_base \
  --setup-margin-ps 100 --timing-guard-ps 50 --max-trials 500 \
  --output-dir "$WORK/pass3a_routed_idle_trial_repro" \
  "${libs[@]}" \
  --lef "$PLATFORM/lef/asap7sc7p5t_28_R_1x_220121a.lef" \
  --lef "$PLATFORM/lef/asap7sc7p5t_28_L_1x_220121a.lef" \
  --lef "$PLATFORM/lef/asap7sc7p5t_28_SL_1x_220121a.lef"
```

The exact candidate netlist was produced from the accepted-swap report, checked by the ASAP7 Yosys adapter, and differentially simulated to create paired gate-level VCDs. The universal measurement runner compared both ODBs using the same SDC and SPEF, the three VCD workloads, 50 ps timing guard, route log, candidate PDN connectivity log, and LEC status/log. The final sign-off manifest is `build/power/phase3a_asap7_gcd_500ps/measured_signoff_final_3a/cl_ovps_manifest.json`; pass reports are under `build/power/phase3a_asap7_gcd_500ps/pass3a_routed_idle_trial_15lib/`.

The Stage 3 injection was also smoke-tested through `--enable-pass-3a` with a flow plan that copied a routed input checkpoint through the four stage slots and ran the actual Stage 3 insertion. The output is under `build/power/phase3a_asap7_gcd_500ps/universal_pass3a_integration_smoke_v3/`. This validates option injection and Stage 3 output handoff, but is not a cold Stage 1–4 ORFS run. An earlier 78-swap pre-route experiment (before the instance-slack unit correction) followed by fresh CTS/routing increased idle dynamic power by 1.297 µW, while typical and peak fell by 126 nW and 1.501 µW. That different candidate is excluded from the 52-swap result; it demonstrates why final multi-workload power must be measured after route.

## Limits

- Dynamic savings on GCD are modest: idle is effectively unchanged at −4.8 nW. The larger relative benefit is leakage reduction on this low-leakage block.
- One VCD is used for the in-memory dynamic guard. Final measurement checks idle, typical, and peak separately; accepting a Stage 3 candidate from one workload does not prove the other workloads before sign-off.
- The final power result applies VT swaps to an existing routed checkpoint with its baseline SPEF; it does not quantify timing-driven route changes after a full fresh route.
- Multi-clock, gated-clock, macro, power-state, and other ASAP7 variants were not evaluated.
