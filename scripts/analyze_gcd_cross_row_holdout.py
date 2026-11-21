#!/usr/bin/env python3
"""Evaluate the frozen cross-row GCD power-placement holdout."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path

from analyze_gcd_cts_branch_proxy import (
    cts_cell_masters,
    extract_cts_cell_power,
    kendall_tau_b,
    read_tsv,
    spearman,
    write_tsv,
)


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
CTS_POWER_TCL = ROOT / "scripts/gcd_report_cts_cell_power.tcl"
RESULTS_ROOT = ROOT / "build/orfs/results/sky130hd/gcd_power"
ACTIVITY_ROOT = ROOT / "build/activity"


def confusion(rows: list[dict[str, object]], predicted: str, actual: str) -> dict[str, object]:
    tp = sum(bool(row[predicted]) and bool(row[actual]) for row in rows)
    fp = sum(bool(row[predicted]) and not bool(row[actual]) for row in rows)
    fn = sum(not bool(row[predicted]) and bool(row[actual]) for row in rows)
    tn = sum(not bool(row[predicted]) and not bool(row[actual]) for row in rows)
    return {
        "n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else math.nan,
        "recall": tp / (tp + fn) if tp + fn else math.nan,
        "specificity": tn / (tn + fp) if tn + fp else math.nan,
    }


def fmt(value: object, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "NA" if math.isnan(number) else f"{number:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    pred_rows = read_tsv(analysis / "frozen_tiered_predictions.tsv")
    routes = read_tsv(experiment / "route_manifest.tsv")
    route_rows = {row["candidate_id"]: row for row in routes}
    candidate_rows = read_tsv(analysis / "candidate_summary.tsv")
    candidate_by = {row["candidate_id"]: row for row in candidate_rows}
    power_rows = read_tsv(analysis / "workload_power.tsv")
    power_by = {(row["candidate_id"], row["workload"]): row for row in power_rows}
    predictions = {row["candidate_id"]: row for row in pred_rows}
    candidate_ids = set(predictions)
    if candidate_ids != set(route_rows) or candidate_ids != set(candidate_by):
        raise ValueError("frozen predictions, route manifest, and route analysis candidate IDs differ")
    if len(candidate_ids) != 82:
        raise ValueError(f"expected 82 routed holdout candidates, found {len(candidate_ids)}")
    if any(int(row["return_code"]) != 0 for row in routes):
        raise ValueError("one or more route commands failed")

    # Verify all files hashed in the pre-route freeze before using route labels.
    frozen_hash_rows = read_tsv(analysis / "pre_route_score_integrity.tsv")
    import hashlib
    hash_failures = []
    for row in frozen_hash_rows:
        if row["path"] == "score_parameters":
            continue
        path = Path(row["path"])
        expected = row.get("sha256", "")
        if not expected or not path.is_file():
            hash_failures.append(str(path))
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            hash_failures.append(str(path))
    if hash_failures:
        raise ValueError("pre-route frozen artifact hash changed: " + ", ".join(hash_failures[:5]))

    baseline_result = RESULTS_ROOT / "gcd_baseline20ns"
    baseline_cells = cts_cell_masters(baseline_result / "6_final.v")
    baseline_power = extract_cts_cell_power(
        OPENROAD, CTS_POWER_TCL, baseline_result,
        ACTIVITY_ROOT / "gcd_baseline20ns/routed/idle.vcd",
        analysis / "routed_cts_cell_power/baseline_idle.log",
    )
    baseline_dummy_internal = sum(
        row[0] for name, row in baseline_power.items() if name.startswith("clkload")
    )
    baseline_clock_internal = sum(row[0] for row in baseline_power.values())
    baseline_clock_switching = sum(row[1] for row in baseline_power.values())

    cached_power_path = analysis / "routed_cts_cell_power.tsv"
    routed_cts_rows: list[dict[str, object]] = read_tsv(cached_power_path) if cached_power_path.is_file() else []
    cached_ids = {row["candidate_id"] for row in routed_cts_rows}
    use_cached_power = cached_ids == candidate_ids
    cached_by_candidate: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    cached_masters: dict[str, dict[str, str]] = {}
    if use_cached_power:
        for row in routed_cts_rows:
            candidate_id = row["candidate_id"]
            instance = row["instance"]
            cached_by_candidate.setdefault(candidate_id, {})[instance] = (
                float(row["internal_nw"]) / 1e9,
                float(row["switching_nw"]) / 1e9,
                float(row["leakage_nw"]) / 1e9,
                float(row["total_nw"]) / 1e9,
            )
            cached_masters.setdefault(candidate_id, {})[instance] = row["master"]
    decision_rows: list[dict[str, object]] = []
    for candidate_id in sorted(candidate_ids):
        route = route_rows[candidate_id]
        variant = route["route_variant"]
        result_dir = RESULTS_ROOT / variant
        vcd = ACTIVITY_ROOT / variant / "candidate/idle.vcd"
        if use_cached_power:
            cell_power = cached_by_candidate[candidate_id]
            masters = cached_masters[candidate_id]
        else:
            cell_power = extract_cts_cell_power(
                OPENROAD, CTS_POWER_TCL, result_dir, vcd,
                analysis / "routed_cts_cell_power" / f"{candidate_id}_idle.log",
            )
            masters = cts_cell_masters(result_dir / "6_final.v")
        dummy_names = {name for name in set(baseline_cells) | set(masters) if name.startswith("clkload")}
        changed = sorted(name for name in dummy_names if baseline_cells.get(name) != masters.get(name))
        base_dummy_internal = sum(baseline_power.get(name, (0, 0, 0, 0))[0] for name in dummy_names)
        cand_dummy_internal = sum(cell_power.get(name, (0, 0, 0, 0))[0] for name in dummy_names)
        base_dummy_switching = sum(baseline_power.get(name, (0, 0, 0, 0))[1] for name in dummy_names)
        cand_dummy_switching = sum(cell_power.get(name, (0, 0, 0, 0))[1] for name in dummy_names)
        cand_clock_internal = sum(row[0] for row in cell_power.values())
        cand_clock_switching = sum(row[1] for row in cell_power.values())
        if not use_cached_power:
            for instance, value in sorted(cell_power.items()):
                routed_cts_rows.append({
                    "candidate_id": candidate_id,
                    "instance": instance,
                    "master": masters.get(instance, ""),
                    "internal_nw": value[0] * 1e9,
                    "switching_nw": value[1] * 1e9,
                    "leakage_nw": value[2] * 1e9,
                    "total_nw": value[3] * 1e9,
                })
        prediction = predictions[candidate_id]
        summary = candidate_by[candidate_id]
        hpwl = float(prediction["signal_hpwl_delta_pct"])
        dummy_internal_delta_nw = (cand_dummy_internal - base_dummy_internal) * 1e9
        cts_internal_delta_nw = (cand_clock_internal - baseline_clock_internal) * 1e9
        cts_switching_delta_nw = (cand_clock_switching - baseline_clock_switching) * 1e9
        actual_dummy_event = bool(changed) and dummy_internal_delta_nw <= -1000.0
        decision: dict[str, object] = {
            "candidate_id": candidate_id,
            "instance": prediction["instance"],
            "branch_before": prediction["branch_before"],
            "signal_hpwl_delta_pct": hpwl,
            "frozen_gate_decision": prediction["frozen_gate_decision"],
            "predicted_favorable_dummy_event": int(prediction["tier1_favorable_dummy_event"]),
            "predicted_tier1_pass": int(prediction["tier1_gate_pass"]),
            "predicted_tier2_pass": int(prediction["tier2_gate_pass"]),
            "routed_dummy_master_changes": ";".join(
                f"{name}:{baseline_cells.get(name, 'ABSENT')}->{masters.get(name, 'ABSENT')}" for name in changed
            ),
            "routed_dummy_master_change_count": len(changed),
            "routed_dummy_internal_delta_nw": dummy_internal_delta_nw,
            "routed_dummy_switching_delta_nw": (cand_dummy_switching - base_dummy_switching) * 1e9,
            "routed_cts_internal_delta_nw": cts_internal_delta_nw,
            "routed_cts_switching_delta_nw": cts_switching_delta_nw,
            "routed_favorable_dummy_event": int(actual_dummy_event),
            "routed_timing_guard": summary["timing_guard"],
            "routed_spef_acceptance": summary["spef_acceptance"],
            "hpwl_rescue_window_0p5_to_1p5": int(0.5 < hpwl <= 1.5),
        }
        for workload in WORKLOADS:
            routed = power_by[(candidate_id, workload)]
            predicted_net = float(prediction[f"combined_{workload}_net_switching_score_nw"])
            actual_net = float(routed["switching_delta_nw"])
            actual_dynamic = float(routed["dynamic_delta_nw"])
            decision[f"predicted_{workload}_net_delta_nw"] = predicted_net
            decision[f"routed_{workload}_net_delta_nw"] = actual_net
            decision[f"routed_{workload}_signal_delta_nw"] = float(routed["signal_delta_nw"])
            decision[f"routed_{workload}_clock_delta_nw"] = float(routed["clock_delta_nw"])
            decision[f"predicted_{workload}_dynamic_delta_nw"] = float(prediction[f"combined_{workload}_dynamic_score_nw"])
            decision[f"routed_{workload}_dynamic_delta_nw"] = actual_dynamic
            decision[f"predicted_{workload}_net_benefit"] = int(predicted_net < 0)
            decision[f"routed_{workload}_net_benefit"] = int(actual_net < 0)
        decision["routed_all_workload_net_benefit"] = int(all(
            float(power_by[(candidate_id, w)]["switching_delta_nw"]) < 0 for w in WORKLOADS
        ))
        decision["tier2_eligible_by_structure"] = int(not int(prediction["dummy_master_change_count"]) and hpwl <= 0.5)
        decision_rows.append(decision)

    write_tsv(analysis / "routed_cts_cell_power.tsv", routed_cts_rows)
    write_tsv(analysis / "cross_row_holdout_decisions.tsv", decision_rows)

    confusion_rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        all_rows = [{
            "pred": bool(row[f"predicted_{workload}_net_benefit"]),
            "actual": bool(row[f"routed_{workload}_net_benefit"]),
        } for row in decision_rows]
        metrics = confusion(all_rows, "pred", "actual")
        confusion_rows.append({"evaluation": "combined_score_sign_all_candidates", "workload": workload, **metrics})
        eligible = [row for row in decision_rows if row["tier2_eligible_by_structure"]]
        eligible_rows = [{
            "pred": bool(float(row[f"predicted_{workload}_net_delta_nw"]) < 0),
            "actual": bool(float(row[f"routed_{workload}_net_delta_nw"]) < 0),
        } for row in eligible]
        confusion_rows.append({"evaluation": "combined_score_sign_tier2_structural_eligible", "workload": workload,
                               **confusion(eligible_rows, "pred", "actual")})

    tier2_eligible = [row for row in decision_rows if row["tier2_eligible_by_structure"]]
    tier2_gate_rows = [{
        "pred": bool(row["predicted_tier2_pass"]),
        "actual": bool(row["routed_all_workload_net_benefit"]) and row["routed_timing_guard"] == "PASS",
    } for row in tier2_eligible]
    confusion_rows.append({"evaluation": "tier2_gate_all_workloads_and_timing_on_eligible_subset", "workload": "all",
                           **confusion(tier2_gate_rows, "pred", "actual")})
    tier1_rows = [{
        "pred": bool(row["predicted_favorable_dummy_event"]),
        "actual": bool(row["routed_favorable_dummy_event"]),
    } for row in decision_rows]
    confusion_rows.append({"evaluation": "tier1_favorable_dummy_event", "workload": "idle_cts_cell_power",
                           **confusion(tier1_rows, "pred", "actual")})
    write_tsv(analysis / "cross_row_holdout_confusion.tsv", confusion_rows)

    rank_rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        for label, pred_key, actual_key in (
            ("combined_net_switching_score_vs_routed_net_switching", f"predicted_{workload}_net_delta_nw", f"routed_{workload}_net_delta_nw"),
            ("combined_dynamic_score_vs_routed_dynamic", f"predicted_{workload}_dynamic_delta_nw", f"routed_{workload}_dynamic_delta_nw"),
        ):
            predicted = [float(row[pred_key]) for row in decision_rows]
            actual = [float(row[actual_key]) for row in decision_rows]
            rank_rows.append({
                "comparison": label,
                "workload": workload,
                "n": len(predicted),
                "spearman_rho": spearman(predicted, actual),
                "kendall_tau_b": kendall_tau_b(predicted, actual),
            })
    # The frozen clock-wire estimate is also compared directly against route/SPEF labels.
    for label, predicted, actual in (
        ("flute_clock_wire_score_vs_routed_clock_switching", "flute_clock_wire_delta_nw", "routed_typical_clock_delta_nw"),
        ("flute_leaf_cap_vs_final_spef_clock_cap", "flute_leaf_delta_cap_ff", "final_spef_clock_cap_delta_ff"),
    ):
        actual_values = (
            [float(row[actual]) for row in decision_rows]
            if actual.startswith("routed_")
            else [float(candidate_by[row["candidate_id"]][actual]) for row in decision_rows]
        )
        rank_rows.append({
            "comparison": label,
            "workload": "typical",
            "n": len(decision_rows),
            "spearman_rho": spearman([float(predictions[row["candidate_id"]][predicted]) for row in decision_rows], actual_values),
            "kendall_tau_b": kendall_tau_b([float(predictions[row["candidate_id"]][predicted]) for row in decision_rows], actual_values),
        })
    write_tsv(analysis / "cross_row_holdout_rank_metrics.tsv", rank_rows)

    all_three_wins = [row for row in decision_rows if row["routed_all_workload_net_benefit"]]
    timing_pass = sum(row["routed_timing_guard"] == "PASS" for row in decision_rows)
    rescue_window = [row for row in decision_rows if row["hpwl_rescue_window_0p5_to_1p5"]]
    rescue_hits = [row for row in rescue_window if row["routed_favorable_dummy_event"] and row["routed_timing_guard"] == "PASS"]
    rescue_large_hits = [row for row in rescue_hits if float(row["routed_dummy_internal_delta_nw"]) <= -3000.0]
    large_dummy_hits = [row for row in decision_rows if float(row["routed_dummy_internal_delta_nw"]) <= -3000.0]
    actual_dummy_events = sum(bool(row["routed_favorable_dummy_event"]) for row in decision_rows)
    tier1_pass_count = sum(bool(row["predicted_tier1_pass"]) for row in decision_rows)
    tier2_pass_count = sum(bool(row["predicted_tier2_pass"]) for row in decision_rows)
    rejects = len(decision_rows) - tier1_pass_count - tier2_pass_count
    metrics_by = {(row["evaluation"], row["workload"]): row for row in confusion_rows}
    lines = [
        "# Frozen GCD cross-row holdout results", "",
        "The 82-candidate pool covers all 35 sinks and four CTS leaves. Pre-route score and gate predictions were frozen and hash-checked before detailed routing. All candidates, including frozen-gate rejects, were routed.", "",
        f"- Candidates routed: {len(decision_rows)}/82; route command failures: 0.",
        f"- Timing guard passes: {timing_pass}/82.",
        f"- Frozen decisions: Tier 1 {tier1_pass_count}, Tier 2 {tier2_pass_count}, reject {rejects}.",
        f"- Actual net-switching reduction in all three workloads: {len(all_three_wins)}/82.",
        f"- Net-switching reductions by workload: idle {sum(float(power_by[(row['candidate_id'], 'idle')]['switching_delta_nw']) < 0 for row in decision_rows)}/82, typical {sum(float(power_by[(row['candidate_id'], 'typical')]['switching_delta_nw']) < 0 for row in decision_rows)}/82, peak {sum(float(power_by[(row['candidate_id'], 'peak')]['switching_delta_nw']) < 0 for row in decision_rows)}/82.",
        f"- Corrected detailed-route acceptance gate passes: {sum(row['routed_spef_acceptance']=='PASS' for row in decision_rows)}/82.",
        "  This flow gate uses internal + net switching dynamic power, leakage, timing, and complete annotation; it is not a net-switching-only gate.",
        "- VCD coverage was accepted only when both baseline and candidate had zero unannotated pins; CTS dummy removal can change the annotated-pin count.",
        "",
        "## Tier 1 dummy-load result", "",
        f"Routed CTS cell-power attribution found {actual_dummy_events} candidates with a changed dummy master and at least 1 µW lower dummy internal power. {len(large_dummy_hits)} had at least 3 µW lower dummy internal power.",
        f"The +0.5% to +1.5% signal-HPWL rescue window contained {len(rescue_window)} candidates; {len(rescue_hits)} produced a favorable routed dummy-power event and passed timing, including {len(rescue_large_hits)} with at least 3 µW dummy internal-power reduction.",
        f"Routed dummy internal-power delta ranged from {min(float(row['routed_dummy_internal_delta_nw']) for row in decision_rows):+.1f} to {max(float(row['routed_dummy_internal_delta_nw']) for row in decision_rows):+.1f} nW (median {statistics.median(float(row['routed_dummy_internal_delta_nw']) for row in decision_rows):+.1f} nW).",
        "",
        "| Candidate | HPWL Δ | Dummy internal Δ | Idle net Δ | Typical net Δ | Peak net Δ | Typical total dynamic Δ | Timing |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rescue_large_hits:
        lines.append(
            f"| {row['candidate_id']} | {float(row['signal_hpwl_delta_pct']):+.3f}% | {float(row['routed_dummy_internal_delta_nw']):+.1f} nW | {float(row['routed_idle_net_delta_nw']):+.1f} nW | {float(row['routed_typical_net_delta_nw']):+.1f} nW | {float(row['routed_peak_net_delta_nw']):+.1f} nW | {float(row['routed_typical_dynamic_delta_nw']):+.1f} nW | {row['routed_timing_guard']} |"
        )
    lines += [
        "",
        "| Workload | Score-sign TP / FP / FN / TN | Precision | Recall | Spearman ρ net | Kendall τ net | Spearman ρ dynamic | Kendall τ dynamic |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for workload in WORKLOADS:
        conf = metrics_by[("combined_score_sign_all_candidates", workload)]
        net_rank = next(row for row in rank_rows if row["comparison"] == "combined_net_switching_score_vs_routed_net_switching" and row["workload"] == workload)
        dyn_rank = next(row for row in rank_rows if row["comparison"] == "combined_dynamic_score_vs_routed_dynamic" and row["workload"] == workload)
        lines.append(
            f"| {workload} | {conf['tp']} / {conf['fp']} / {conf['fn']} / {conf['tn']} | {fmt(conf['precision'])} | {fmt(conf['recall'])} | {fmt(net_rank['spearman_rho'])} | {fmt(net_rank['kendall_tau_b'])} | {fmt(dyn_rank['spearman_rho'])} | {fmt(dyn_rank['kendall_tau_b'])} |"
        )
    gate_conf = metrics_by[("tier2_gate_all_workloads_and_timing_on_eligible_subset", "all")]
    tier1_conf = metrics_by[("tier1_favorable_dummy_event", "idle_cts_cell_power")]
    lines += [
        "", "## Gate precision and recall", "",
        f"On the structurally eligible Tier 2 subset (no frozen dummy change, HPWL ≤ +0.5%), the all-workload net-saving gate had {gate_conf['tp']} TP, {gate_conf['fp']} FP, {gate_conf['fn']} FN, {gate_conf['tn']} TN; precision {fmt(gate_conf['precision'])}, recall {fmt(gate_conf['recall'])}.",
        f"Tier 1 favorable-dummy event prediction had {tier1_conf['tp']} TP, {tier1_conf['fp']} FP, {tier1_conf['fn']} FN, {tier1_conf['tn']} TN against routed per-cell dummy internal power.",
        "",
        "Tier 2 structural-eligible sign accuracy by workload:",
        "",
        "| Workload | N | TP / FP / FN / TN | Precision | Recall |",
        "|---|---:|---:|---:|---:|",
    ]
    for workload in WORKLOADS:
        conf = metrics_by[("combined_score_sign_tier2_structural_eligible", workload)]
        lines.append(
            f"| {workload} | {conf['n']} | {conf['tp']} / {conf['fp']} / {conf['fn']} / {conf['tn']} | {fmt(conf['precision'])} | {fmt(conf['recall'])} |"
        )
    flute_wire = next(row for row in rank_rows if row["comparison"] == "flute_clock_wire_score_vs_routed_clock_switching")
    flute_cap = next(row for row in rank_rows if row["comparison"] == "flute_leaf_cap_vs_final_spef_clock_cap")
    lines += [
        "",
        "Tier 2 score-sign confusion is computed separately per workload against routed total net switching (clock plus signal). Candidate-level signal-only and clock-only deltas are retained in `cross_row_holdout_decisions.tsv`.",
        f"FLUTE clock-wire estimate vs routed clock-net switching: Spearman ρ={fmt(flute_wire['spearman_rho'])}, Kendall τ={fmt(flute_wire['kendall_tau_b'])}; vs final SPEF clock-cap delta: Spearman ρ={fmt(flute_cap['spearman_rho'])}.",
        "No FLUTE damping factor or score coefficient was fitted on this holdout.",
        "",
        "## Output files", "",
        "- `frozen_tiered_predictions.tsv`: frozen pre-route predictions and pass/reject labels.",
        "- `cross_row_holdout_decisions.tsv`: frozen predictions joined with routed outcomes.",
        "- `cross_row_holdout_confusion.tsv`: precision/recall confusion counts.",
        "- `cross_row_holdout_rank_metrics.tsv`: Spearman and Kendall rank correlations.",
        "- `routed_cts_cell_power.tsv`: exact post-route clock buffer and dummy cell attribution.",
        "- `candidate_summary.tsv`, `workload_power.tsv`, and `summary.md`: route/SPEF/power/timing details.",
    ]
    (analysis / "cross_row_holdout_report.md").write_text("\n".join(lines) + "\n")
    print(f"GCD_CROSS_ROW_HOLDOUT_ANALYSIS candidates={len(decision_rows)} tier1_dummy_events={actual_dummy_events} tier2_wins_all_workloads={len(all_three_wins)} output={analysis / 'cross_row_holdout_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
