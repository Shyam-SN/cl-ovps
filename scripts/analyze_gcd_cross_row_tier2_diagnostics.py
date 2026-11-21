#!/usr/bin/env python3
"""Diagnose Tier 2 errors and compare damped FLUTE/GRT estimates on the frozen holdout.

All damping and threshold results are in-sample diagnostics only. They must not
be promoted into the frozen placer score without a new independent validation.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXP = ROOT / "build/power/gcd_flop_relocation_cross_row_holdout_20261029a"
VOLTAGE = 1.8
FACTOR = 0.5 * VOLTAGE**2
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty table {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for r in rows for k in r)),
                                delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def average_ranks(values: list[float]) -> list[float]:
    ordering = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordering):
        end = start + 1
        while end < len(ordering) and values[ordering[end]] == values[ordering[start]]:
            end += 1
        rank = (start + end - 1) / 2.0 + 1.0
        for j in range(start, end):
            ranks[ordering[j]] = rank
        start = end
    return ranks


def pearson(x: list[float], y: list[float]) -> float | None:
    if len(x) != len(y) or len(x) < 2:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = sum((v - mx) ** 2 for v in x)
    sy = sum((v - my) ** 2 for v in y)
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sx * sy)


def spearman(x: list[float], y: list[float]) -> float | None:
    return pearson(average_ranks(x), average_ranks(y))


def kendall_tau_b(x: list[float], y: list[float]) -> float | None:
    concordant = discordant = ties_x = ties_y = 0
    for i in range(len(x)):
        for j in range(i + 1, len(x)):
            dx, dy = x[i] - x[j], y[i] - y[j]
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif dx * dy > 0:
                concordant += 1
            else:
                discordant += 1
    denom = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    return (concordant - discordant) / denom if denom else None


def fmt(value: float | None, digits: int = 3) -> str:
    return "NA" if value is None else f"{value:.{digits}f}"


def confusion(predicted: set[str], actual: set[str], universe: set[str]) -> dict[str, int | float]:
    tp = len(predicted & actual)
    fp = len(predicted - actual)
    fn = len(actual - predicted)
    tn = len(universe - predicted - actual)
    return {
        "n": len(universe), "accepted": len(predicted), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": tp / (tp + fp) if tp + fp else 0.0,
        "recall": tp / (tp + fn) if tp + fn else 0.0,
    }


def load_grt_caps() -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"clock_w": 0.0, "signal_w": 0.0, "clock_rows": 0.0, "signal_rows": 0.0})
    )
    raw_dir = EXP / "analysis/grt_activity_caps"
    manifest = read_tsv(raw_dir / "manifest.tsv")
    if len(manifest) != 83:
        raise ValueError(f"expected baseline plus 82 GRT reports, found {len(manifest)}")
    for item in manifest:
        candidate = item["candidate_id"]
        for line in (ROOT / item["output"]).read_text().splitlines():
            if not line.startswith("#GRT_DRIVER_CAP\t") or line.endswith("\tactivity_origin"):
                continue
            fields = line.split("\t")[1:]
            if len(fields) != 10:
                raise ValueError(f"bad GRT activity row ({len(fields)} fields): {line}")
            workload, category, _pin, _net, _pin_cap, _wire_cap, total_cap, density, _duty, _origin = fields
            power_w = FACTOR * float(total_cap) * float(density)
            result[candidate][workload][f"{category}_w"] += power_w
            result[candidate][workload][f"{category}_rows"] += 1
    return result


def load_grt_driver_rows() -> dict[str, dict[tuple[str, str], dict[str, float | str]]]:
    result: dict[str, dict[tuple[str, str], dict[str, float | str]]] = {}
    for item in read_tsv(EXP / "analysis/grt_activity_caps/manifest.tsv"):
        candidate = item["candidate_id"]
        records: dict[tuple[str, str], dict[str, float | str]] = {}
        for line in (ROOT / item["output"]).read_text().splitlines():
            if not line.startswith("#GRT_DRIVER_CAP\t") or line.endswith("\tactivity_origin"):
                continue
            fields = line.split("\t")[1:]
            workload, category, pin, _net, _pin_cap, _wire_cap, total_cap, density, _duty, _origin = fields
            records[(workload, pin)] = {
                "category": category, "total_cap_f": float(total_cap), "density_hz": float(density),
            }
        result[candidate] = records
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-grt", action="store_true", help="only run Tier 2 decomposition and threshold sweep")
    args = parser.parse_args()
    analysis = EXP / "analysis"
    decisions = read_tsv(analysis / "cross_row_holdout_decisions.tsv")
    frozen = {r["candidate_id"]: r for r in read_tsv(analysis / "frozen_tiered_predictions.tsv")}
    power = read_tsv(analysis / "workload_power.tsv")
    power_by = {(r["candidate_id"], r["workload"]): r for r in power}
    rows = {r["candidate_id"]: r for r in decisions}
    eligible = {cid for cid, row in rows.items() if row["tier2_eligible_by_structure"] == "1"}
    actual_winners = {cid for cid in eligible if rows[cid]["routed_all_workload_net_benefit"] == "1"}
    frozen_pass = {cid for cid in eligible if rows[cid]["frozen_gate_decision"] == "TIER2_PASS"}

    # Diagnose the exact workload that breaks each current candidate-level FP/FN.
    decomposition: list[dict[str, object]] = []
    error_groups: dict[str, list[dict[str, float]]] = {"FP": [], "FN": []}
    for cid in sorted((frozen_pass - actual_winners) | (actual_winners - frozen_pass)):
        kind = "FP" if cid in frozen_pass else "FN"
        candidates = []
        for workload in WORKLOADS:
            f = frozen[cid]
            d = rows[cid]
            signal_pred = float(f[f"placement_{workload}_switching_delta_nw"])
            clock_pred = float(f["flute_clock_wire_delta_nw"])
            signal_actual = float(d[f"routed_{workload}_signal_delta_nw"])
            clock_actual = float(d[f"routed_{workload}_clock_delta_nw"])
            pred = signal_pred + clock_pred
            actual = signal_actual + clock_actual
            candidates.append({
                "workload": workload, "pred": pred, "actual": actual,
                "signal_error_pred_minus_actual": signal_pred - signal_actual,
                "clock_error_pred_minus_actual": clock_pred - clock_actual,
                "signal_saving_overestimate": signal_actual - signal_pred,
                "clock_saving_overestimate": clock_actual - clock_pred,
                "signal_pred": signal_pred, "clock_pred": clock_pred,
                "signal_actual": signal_actual, "clock_actual": clock_actual,
            })
        if kind == "FP":
            limits = [x for x in candidates if x["pred"] < 0 and x["actual"] >= 0]
            selected = max(limits, key=lambda x: x["actual"] - x["pred"]) if limits else max(candidates, key=lambda x: x["actual"] - x["pred"])
            sig, clk = selected["signal_saving_overestimate"], selected["clock_saving_overestimate"]
            dominant = "signal" if sig > clk else "clock"
        else:
            limits = [x for x in candidates if x["pred"] >= 0 and x["actual"] < 0]
            selected = max(limits, key=lambda x: x["pred"] - x["actual"]) if limits else max(candidates, key=lambda x: x["pred"] - x["actual"])
            sig = selected["signal_error_pred_minus_actual"]
            clk = selected["clock_error_pred_minus_actual"]
            dominant = "signal" if sig > clk else "clock"
        error_groups[kind].append(selected)
        decomposition.append({
            "candidate_id": cid, "classification": kind, "limiting_workload": selected["workload"],
            "predicted_net_delta_nw": selected["pred"], "routed_net_delta_nw": selected["actual"],
            "predicted_signal_delta_nw": selected["signal_pred"], "routed_signal_delta_nw": selected["signal_actual"],
            "predicted_flute_clock_delta_nw": selected["clock_pred"], "routed_clock_delta_nw": selected["clock_actual"],
            "signal_error_pred_minus_actual_nw": selected["signal_error_pred_minus_actual"],
            "clock_error_pred_minus_actual_nw": selected["clock_error_pred_minus_actual"],
            "dominant_error_component": dominant,
        })
    write_tsv(analysis / "tier2_diagnostic_decomposition.tsv", decomposition)

    # In-sample threshold/damping sweep. Tier2 requires each workload to meet margin.
    sweep: list[dict[str, object]] = []
    sweep_prediction_sets: dict[tuple[float, float], set[str]] = {}
    for gamma in (1.0, 0.5, 0.3, 0.2, 0.0):
        for threshold in (0.0, -50.0, -100.0, -150.0, -250.0, -500.0, -750.0):
            predicted: set[str] = set()
            for cid in eligible:
                f = frozen[cid]
                scores = [float(f[f"placement_{workload}_switching_delta_nw"]) + gamma * float(f["flute_clock_wire_delta_nw"])
                          for workload in WORKLOADS]
                if all(score <= threshold for score in scores):
                    predicted.add(cid)
            metrics = confusion(predicted, actual_winners, eligible)
            sweep_prediction_sets[(gamma, threshold)] = predicted
            sweep.append({
                "gamma_flute": gamma, "per_workload_threshold_nw": threshold,
                **metrics,
                "true_positives_retained": metrics["tp"],
                "actual_tier2_winners": len(actual_winners),
                "status": "IN_SAMPLE_DIAGNOSTIC_ONLY",
            })
    write_tsv(analysis / "tier2_damping_sweep.tsv", sweep)

    # Exact scan of the continuous positive-damping range. With an all-workload
    # pass condition, each candidate's score is max(signal workload scores) +
    # gamma*FLUTE, a line in gamma. Pairwise line crossings partition [0,1]
    # into intervals with fixed ordering, so all possible thresholds are tested.
    scalar_lines = {
        cid: (max(float(frozen[cid][f"placement_{w}_switching_delta_nw"]) for w in WORKLOADS),
              float(frozen[cid]["flute_clock_wire_delta_nw"]))
        for cid in eligible
    }
    gamma_breaks = {0.0, 1.0}
    sorted_ids = sorted(eligible)
    for i, cid1 in enumerate(sorted_ids):
        a1, b1 = scalar_lines[cid1]
        for cid2 in sorted_ids[i + 1:]:
            a2, b2 = scalar_lines[cid2]
            if b1 != b2:
                crossing = (a2 - a1) / (b1 - b2)
                if 0.0 < crossing < 1.0:
                    gamma_breaks.add(crossing)
    gamma_breaks = sorted(gamma_breaks)
    gamma_probes = set(gamma_breaks)
    gamma_probes.update((left + right) / 2.0 for left, right in zip(gamma_breaks, gamma_breaks[1:]))
    best_scalar = {"precision": -1.0, "tp": 0, "accepted": 0, "gamma": 0.0, "threshold": 0.0, "ids": []}
    perfect_scalar_separation = False
    for gamma in gamma_probes:
        ordered = sorted((intercept + gamma * slope, cid) for cid, (intercept, slope) in scalar_lines.items())
        tp = accepted = 0
        start = 0
        while start < len(ordered):
            threshold = ordered[start][0]
            end = start
            while end < len(ordered) and abs(ordered[end][0] - threshold) < 1e-9:
                tp += ordered[end][1] in actual_winners
                accepted += 1
                end += 1
            precision_value = tp / accepted
            if precision_value == 1.0 and tp > 0:
                perfect_scalar_separation = True
            if (precision_value > best_scalar["precision"] or
                    (precision_value == best_scalar["precision"] and tp > best_scalar["tp"]) or
                    (precision_value == best_scalar["precision"] and tp == best_scalar["tp"] and accepted < best_scalar["accepted"])):
                best_scalar = {
                    "precision": precision_value, "tp": tp, "accepted": accepted,
                    "gamma": gamma, "threshold": threshold,
                    "ids": [cid for _, cid in ordered[:end]],
                }
            start = end
    class_breaks = {0.0, 1.0}
    negative_winners = eligible - actual_winners
    for group in (actual_winners, negative_winners):
        ids = sorted(group)
        for i, cid1 in enumerate(ids):
            a1, b1 = scalar_lines[cid1]
            for cid2 in ids[i + 1:]:
                a2, b2 = scalar_lines[cid2]
                if b1 != b2:
                    crossing = (a2 - a1) / (b1 - b2)
                    if 0.0 < crossing < 1.0:
                        class_breaks.add(crossing)
    class_breaks = sorted(class_breaks)
    separation_probes = set(class_breaks)
    separation_probes.update((left + right) / 2.0 for left, right in zip(class_breaks, class_breaks[1:]))
    min_separation = None
    for gamma in separation_probes:
        positive_min = min((a + gamma * b, cid) for cid, (a, b) in scalar_lines.items() if cid in actual_winners)
        negative_min = min((a + gamma * b, cid) for cid, (a, b) in scalar_lines.items() if cid not in actual_winners)
        gap = positive_min[0] - negative_min[0]
        if min_separation is None or gap < min_separation["gap_nw"]:
            min_separation = {
                "gap_nw": gap, "gamma": gamma,
                "lowest_positive_score_nw": positive_min[0], "lowest_positive_candidate": positive_min[1],
                "lowest_negative_score_nw": negative_min[0], "lowest_negative_candidate": negative_min[1],
            }
    continuous_row = {
        "gamma_range": "[0,1]", "threshold_search": "all_real_thresholds",
        "candidate_count": len(eligible), "actual_winners": len(actual_winners),
        "pairwise_crossing_count": len(gamma_breaks),
        "best_precision": best_scalar["precision"], "best_tp": best_scalar["tp"],
        "best_fp": best_scalar["accepted"] - best_scalar["tp"],
        "best_fn": len(actual_winners) - best_scalar["tp"],
        "best_tn": len(eligible) - best_scalar["accepted"] - (len(actual_winners) - best_scalar["tp"]),
        "best_recall": best_scalar["tp"] / len(actual_winners),
        "best_gamma": best_scalar["gamma"], "best_threshold_nw": best_scalar["threshold"],
        "best_accepted_candidates": ",".join(best_scalar["ids"]),
        "perfect_precision_with_nonzero_recall_found": int(perfect_scalar_separation),
        **min_separation,
        "status": "IN_SAMPLE_EXACT_CONTINUOUS_SCAN",
    }
    write_tsv(analysis / "tier2_continuous_gamma_scan.tsv", [continuous_row])

    current_counts = confusion(frozen_pass, actual_winners, eligible)
    error_component_counts = {}
    for kind, group in error_groups.items():
        error_component_counts[kind] = {
            "signal": sum(1 for item in group if item["signal_saving_overestimate"] > item["clock_saving_overestimate"] if kind == "FP")
                      + sum(1 for item in group if item["signal_error_pred_minus_actual"] > item["clock_error_pred_minus_actual"] if kind == "FN"),
            "clock": sum(1 for item in group if item["signal_saving_overestimate"] <= item["clock_saving_overestimate"] if kind == "FP")
                     + sum(1 for item in group if item["signal_error_pred_minus_actual"] <= item["clock_error_pred_minus_actual"] if kind == "FN"),
        }

    grt_metrics: list[dict[str, object]] = []
    grt_delta_rows: list[dict[str, object]] = []
    if not args.skip_grt:
        grt = load_grt_caps()
        if set(grt) != set(rows) | {"baseline"}:
            raise ValueError(f"GRT candidates differ: got {len(grt)}, expected {len(rows)+1}")
        for cid in sorted(rows):
            for workload in WORKLOADS:
                item = {"candidate_id": cid, "workload": workload}
                actual = power_by[(cid, workload)]
                for category in ("clock", "signal"):
                    baseline_w = grt["baseline"][workload][f"{category}_w"]
                    candidate_w = grt[cid][workload][f"{category}_w"]
                    item[f"grt_{category}_delta_nw"] = (candidate_w - baseline_w) * 1e9
                    item[f"routed_{category}_delta_nw"] = float(actual[f"{category}_delta_nw"])
                item["grt_total_delta_nw"] = item["grt_clock_delta_nw"] + item["grt_signal_delta_nw"]
                item["routed_total_delta_nw"] = float(actual["switching_delta_nw"])
                flute = float(frozen[cid]["flute_clock_wire_delta_nw"])
                item["flute_clock_wire_delta_nw"] = flute
                grt_delta_rows.append(item)
        write_tsv(analysis / "grt_activity_power_deltas.tsv", grt_delta_rows)
        for workload in WORKLOADS:
            subset = [r for r in grt_delta_rows if r["workload"] == workload]
            for category in ("clock", "signal", "total"):
                pred = [float(r[f"grt_{category}_delta_nw"]) for r in subset]
                actual = [float(r[f"routed_{category}_delta_nw"]) if category != "total" else float(r["routed_total_delta_nw"]) for r in subset]
                grt_metrics.append({
                    "workload": workload, "component": category,
                    "n": len(subset), "spearman_grt_to_routed": spearman(pred, actual),
                    "kendall_grt_to_routed": kendall_tau_b(pred, actual),
                    "sign_matches_excluding_abs_actual_le_0p1nw": sum((a < 0) == (p < 0) for p, a in zip(pred, actual) if abs(a) > 0.1),
                    "sign_cases": sum(1 for a in actual if abs(a) > 0.1),
                    "spearman_flute_clock_to_routed_clock": spearman(
                        [float(r["flute_clock_wire_delta_nw"]) for r in subset],
                        [float(r["routed_clock_delta_nw"]) for r in subset]),
                })
        write_tsv(analysis / "grt_activity_correlation.tsv", grt_metrics)

        # Fixed-activity ablation: use the baseline VCD-derived density for
        # each same-named driver on every candidate's GRT capacitance.
        driver_rows = load_grt_driver_rows()
        baseline_activity = {key: float(value["density_hz"]) for key, value in driver_rows["baseline"].items()}
        if set(driver_rows) != set(grt):
            raise ValueError("GRT raw-driver set differs from the aggregate GRT set")
        fixed_power: dict[str, dict[str, dict[str, float]]] = defaultdict(
            lambda: defaultdict(lambda: {"clock_w": 0.0, "signal_w": 0.0})
        )
        baseline_fixed: dict[str, dict[str, float]] = defaultdict(lambda: {"clock_w": 0.0, "signal_w": 0.0})
        for workload in WORKLOADS:
            for key, record in driver_rows["baseline"].items():
                if key[0] != workload:
                    continue
                category = str(record["category"])
                baseline_fixed[workload][f"{category}_w"] += FACTOR * float(record["total_cap_f"]) * baseline_activity[key]
        for cid, records in driver_rows.items():
            if cid == "baseline":
                continue
            if set(records) != set(baseline_activity):
                raise ValueError(f"fixed-VCD mapping is incomplete for {cid}: {len(set(records) & set(baseline_activity))}/{len(records)} drivers mapped")
            for key, record in records.items():
                workload = key[0]
                category = str(record["category"])
                fixed_power[cid][workload][f"{category}_w"] += FACTOR * float(record["total_cap_f"]) * baseline_activity[key]
        fixed_rows: list[dict[str, object]] = []
        for cid in sorted(rows):
            for workload in WORKLOADS:
                item: dict[str, object] = {"candidate_id": cid, "workload": workload,
                                           "activity_source": "fixed_baseline_vcd"}
                actual = power_by[(cid, workload)]
                for category in ("clock", "signal"):
                    item[f"grt_{category}_delta_nw"] = (fixed_power[cid][workload][f"{category}_w"] - baseline_fixed[workload][f"{category}_w"]) * 1e9
                    item[f"routed_{category}_delta_nw"] = float(actual[f"{category}_delta_nw"])
                item["grt_total_delta_nw"] = float(item["grt_clock_delta_nw"]) + float(item["grt_signal_delta_nw"])
                item["routed_total_delta_nw"] = float(actual["switching_delta_nw"])
                item["flute_clock_wire_delta_nw"] = float(frozen[cid]["flute_clock_wire_delta_nw"])
                fixed_rows.append(item)
        write_tsv(analysis / "grt_fixed_activity_power_deltas.tsv", fixed_rows)
        fixed_corr_rows: list[dict[str, object]] = []
        for subset_name, subset_ids in (("tier2_structural_eligible", eligible), ("all_candidates", set(rows))):
            for workload in WORKLOADS:
                subset = [r for r in fixed_rows if r["workload"] == workload and r["candidate_id"] in subset_ids]
                for category in ("clock", "signal", "total"):
                    pred = [float(r[f"grt_{category}_delta_nw"]) for r in subset]
                    actual = [float(r[f"routed_{category}_delta_nw"]) if category != "total" else float(r["routed_total_delta_nw"]) for r in subset]
                    flute_pred = [float(r["flute_clock_wire_delta_nw"]) for r in subset]
                    clock_actual = [float(r["routed_clock_delta_nw"]) for r in subset]
                    fixed_corr_rows.append({
                        "subset": subset_name, "workload": workload, "component": category,
                        "activity_source": "fixed_baseline_vcd", "n": len(subset),
                        "spearman_grt_to_routed": spearman(pred, actual),
                        "kendall_grt_to_routed": kendall_tau_b(pred, actual),
                        "spearman_flute_clock_to_routed_clock_same_subset": spearman(flute_pred, clock_actual),
                        "kendall_flute_clock_to_routed_clock_same_subset": kendall_tau_b(flute_pred, clock_actual),
                    })
        write_tsv(analysis / "grt_fixed_activity_correlation.tsv", fixed_corr_rows)

        fixed_by = {(str(r["candidate_id"]), str(r["workload"])): r for r in fixed_rows}
        gate_predictions: dict[str, set[str]] = {
            "combined_grt_all_workloads_lt_0": set(),
            "signal_only_all_workloads_le_0": set(),
            "clock_guard_all_workloads_le_0": set(),
            "decoupled_signal_le_0_and_clock_non_degrading": set(),
        }
        candidate_gate_rows: list[dict[str, object]] = []
        for cid in sorted(eligible):
            per_workload = [fixed_by[(cid, w)] for w in WORKLOADS]
            combined_pass = all(float(r["grt_total_delta_nw"]) < 0 for r in per_workload)
            signal_pass = all(float(r["grt_signal_delta_nw"]) <= 0 for r in per_workload)
            clock_pass = all(float(r["grt_clock_delta_nw"]) <= 0 for r in per_workload)
            decoupled_pass = signal_pass and clock_pass
            for name, passed in (("combined_grt_all_workloads_lt_0", combined_pass),
                                 ("signal_only_all_workloads_le_0", signal_pass),
                                 ("clock_guard_all_workloads_le_0", clock_pass),
                                 ("decoupled_signal_le_0_and_clock_non_degrading", decoupled_pass)):
                if passed:
                    gate_predictions[name].add(cid)
            candidate_gate_rows.append({
                "candidate_id": cid,
                "tier2_eligible": 1,
                "predicted_combined_grt_pass": int(combined_pass),
                "predicted_signal_only_pass": int(signal_pass),
                "predicted_clock_non_degrading": int(clock_pass),
                "predicted_decoupled_pass": int(decoupled_pass),
                "actual_all_workload_signal_saving": int(all(float(rows[cid][f"routed_{w}_signal_delta_nw"]) < 0 for w in WORKLOADS)),
                "actual_all_workload_net_saving": int(rows[cid]["routed_all_workload_net_benefit"] == "1"),
            })
        write_tsv(analysis / "grt_decoupled_gate_candidates.tsv", candidate_gate_rows)
        gate_metric_rows: list[dict[str, object]] = []
        actual_signal_winners = {r["candidate_id"] for r in candidate_gate_rows if r["actual_all_workload_signal_saving"] == 1}
        for policy, selected in gate_predictions.items():
            for target_name, target in (("all_workload_net_saving", actual_winners),
                                        ("all_workload_signal_saving", actual_signal_winners)):
                gate_metric_rows.append({
                    "policy": policy, "target": target_name,
                    "decision_basis": "fixed_baseline_vcd_gated_on_all_3_workloads",
                    **confusion(selected, target, eligible),
                })
        write_tsv(analysis / "grt_decoupled_gate_metrics.tsv", gate_metric_rows)
        predictor_corr_rows: list[dict[str, object]] = []
        predictor_specs = (
            ("fixed_baseline_vcd_grt_signal", "grt_signal_delta_nw", "routed_signal_delta_nw"),
            ("placement_vcd_signal_score", "placement_signal_score_nw", "routed_signal_delta_nw"),
            ("signal_hpwl_delta_pct", "signal_hpwl_delta_pct", "routed_signal_delta_nw"),
            ("fixed_baseline_vcd_grt_clock", "grt_clock_delta_nw", "routed_clock_delta_nw"),
            ("flute_clock_wire_score", "flute_clock_wire_delta_nw", "routed_clock_delta_nw"),
            ("fixed_baseline_vcd_grt_total", "grt_total_delta_nw", "routed_total_delta_nw"),
            ("frozen_placement_plus_flute_total", "frozen_total_score_nw", "routed_total_delta_nw"),
        )
        for workload in WORKLOADS:
            subset = [cid for cid in sorted(eligible)]
            for label, predictor_key, actual_key in predictor_specs:
                predictor_values: list[float] = []
                actual_values: list[float] = []
                for cid in subset:
                    fixed = fixed_by[(cid, workload)]
                    decision = rows[cid]
                    frozen_row = frozen[cid]
                    if predictor_key == "placement_signal_score_nw":
                        predicted = float(frozen_row[f"placement_{workload}_switching_delta_nw"])
                    elif predictor_key == "signal_hpwl_delta_pct":
                        predicted = float(decision["signal_hpwl_delta_pct"])
                    elif predictor_key == "flute_clock_wire_delta_nw":
                        predicted = float(frozen_row["flute_clock_wire_delta_nw"])
                    elif predictor_key == "frozen_total_score_nw":
                        predicted = float(frozen_row[f"combined_{workload}_net_switching_score_nw"])
                    else:
                        predicted = float(fixed[predictor_key])
                    if actual_key == "routed_signal_delta_nw":
                        actual = float(decision[f"routed_{workload}_signal_delta_nw"])
                    elif actual_key == "routed_clock_delta_nw":
                        actual = float(decision[f"routed_{workload}_clock_delta_nw"])
                    else:
                        actual = float(decision[f"routed_{workload}_net_delta_nw"])
                    predictor_values.append(predicted)
                    actual_values.append(actual)
                predictor_corr_rows.append({
                    "subset": "tier2_structural_eligible", "n": len(subset), "workload": workload,
                    "predictor": label, "target": actual_key,
                    "spearman": spearman(predictor_values, actual_values),
                    "kendall_tau_b": kendall_tau_b(predictor_values, actual_values),
                })
        write_tsv(analysis / "tier2_predictor_component_correlation.tsv", predictor_corr_rows)

    lines = [
        "# Tier 2 cross-row diagnostic",
        "",
        "All score sweeps below reuse this holdout and are in-sample diagnostics. They are not validation evidence and must not change the frozen placer gate. The GRT exporter uses candidate VCDs generated from completed routes, so its VCD-aware results are post-route activity-conditioned diagnostics, not legal pre-route features.",
        "",
        f"Tier 2 structural subset: {len(eligible)} candidates; actual all-workload net-switching winners: {len(actual_winners)}.",
        f"Frozen gamma=1, threshold=0 gate: {current_counts['tp']} TP, {current_counts['fp']} FP, {current_counts['fn']} FN, {current_counts['tn']} TN; precision {current_counts['precision']:.3f}, recall {current_counts['recall']:.3f}.",
        "",
        "## False-positive / false-negative attribution",
        "",
        f"For the workload that most strongly breaks each candidate decision, the dominant component of the prediction error was signal for {error_component_counts['FP']['signal']} of {len(error_groups['FP'])} false positives and clock for {error_component_counts['FP']['clock']}; among false negatives it was signal for {error_component_counts['FN']['signal']} of {len(error_groups['FN'])} and clock for {error_component_counts['FN']['clock']}. See `tier2_diagnostic_decomposition.tsv` for candidate-level predictions, routed components, and errors.",
        "",
        "| Class | Candidate | Limiting workload | Pred. signal | Actual signal | Pred. FLUTE clock | Actual clock | Pred. total | Actual total | Dominant error |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in decomposition:
        lines.append(f"| {row['classification']} | {row['candidate_id']} | {row['limiting_workload']} | {float(row['predicted_signal_delta_nw']):+.1f} | {float(row['routed_signal_delta_nw']):+.1f} | {float(row['predicted_flute_clock_delta_nw']):+.1f} | {float(row['routed_clock_delta_nw']):+.1f} | {float(row['predicted_net_delta_nw']):+.1f} | {float(row['routed_net_delta_nw']):+.1f} | {row['dominant_error_component']} |")
    lines += ["", "## FLUTE damping and confidence-margin sweep", "", "Tier 2 prediction passes only if every workload score `placement signal + gamma × FLUTE clock` is at or below the listed threshold.", "", "| γ | Threshold (nW/workload) | Accepted | TP | FP | FN | TN | Precision | Recall |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for row in sweep:
        lines.append(f"| {float(row['gamma_flute']):.1f} | {float(row['per_workload_threshold_nw']):.0f} | {int(row['accepted'])} | {int(row['tp'])} | {int(row['fp'])} | {int(row['fn'])} | {int(row['tn'])} | {float(row['precision']):.3f} | {float(row['recall']):.3f} |")
    lines += ["", "Strong actual winners retained by the proposed γ=0.2–0.3, −150 nW threshold:"]
    for gamma in (0.2, 0.3):
        selected = sorted(sweep_prediction_sets[(gamma, -150.0)])
        hits = sorted(set(selected) & actual_winners)
        lines.append(f"- γ={gamma:.1f}: {len(hits)}/{len(actual_winners)} actual all-workload winners retained ({', '.join(hits) if hits else 'none'}); {len(selected)} total candidates accepted.")
    selected = sorted(sweep_prediction_sets[(1.0, -150.0)])
    hits = sorted(set(selected) & actual_winners)
    lines.append(f"- γ=1.0: {len(hits)}/{len(actual_winners)} actual all-workload winners retained ({', '.join(hits) if hits else 'none'}); {len(selected)} total candidates accepted, including {len(selected)-len(hits)} false positives.")
    lines.append("- Across this tested grid, the best precision was only 0.286 (γ=1.0, −50 nW), at 4/17 recall; damping FLUTE to γ=0.2–0.3 did not make the −150 nW threshold viable.")
    lines.append(f"- Exact continuous scan over every γ in [0,1] and every threshold found no FP-free separating threshold. Its best precision was {float(best_scalar['precision']):.3f} ({best_scalar['tp']}/{best_scalar['accepted']} accepted, recall {float(best_scalar['tp'])/len(actual_winners):.3f}) at γ={float(best_scalar['gamma']):.6g}, threshold {float(best_scalar['threshold']):+.3f} nW; accepted: {', '.join(best_scalar['ids'])}.")
    lines.append(f"- The minimum lower-envelope gap was {float(min_separation['gap_nw']):+.3f} nW at γ={float(min_separation['gamma']):.6g}: the lowest-scoring actual winner was {min_separation['lowest_positive_candidate']} ({float(min_separation['lowest_positive_score_nw']):+.3f} nW), while a non-winner {min_separation['lowest_negative_candidate']} scored lower ({float(min_separation['lowest_negative_score_nw']):+.3f} nW).")
    if not args.skip_grt:
        lines += ["", "## Global-route parasitics compared with detailed SPEF power", "", "Each candidate's GRT capacitance was weighted by that candidate's VCD activity; the baseline used the baseline routed VCD. These are delta switching estimates from the GRT checkpoint. Rank correlation is across 82 candidates; FLUTE is the existing wire-only estimate.", "", "| Workload | Component | GRT ρ | GRT τ-b | GRT sign matches | FLUTE clock ρ |", "|---|---|---:|---:|---:|---:|"]
        for row in grt_metrics:
            lines.append(f"| {row['workload']} | {row['component']} | {fmt(row['spearman_grt_to_routed'])} | {fmt(row['kendall_grt_to_routed'])} | {row['sign_matches_excluding_abs_actual_le_0p1nw']}/{row['sign_cases']} | {fmt(row['spearman_flute_clock_to_routed_clock'])} |")
        fixed_corr = read_tsv(analysis / "grt_fixed_activity_correlation.tsv")
        lines += ["", "## Fixed-baseline-VCD FastRoute ablation", "", "For this comparison, the same baseline workload transition density was applied to each candidate's GRT capacitance by matching the same driver pin. All 957 driver/workload records mapped for every candidate, including the 68 Tier 2 structural candidates. This removes candidate-specific VCD values and is a closer proxy for a frozen workload profile before placement.", "", "| Subset | Workload | Component | N | GRT ρ | GRT τ-b | FLUTE clock ρ on same subset |", "|---|---|---|---:|---:|---:|---:|"]
        for row in fixed_corr:
            lines.append(f"| {row['subset']} | {row['workload']} | {row['component']} | {row['n']} | {fmt(float(row['spearman_grt_to_routed']) if row['spearman_grt_to_routed'] else None)} | {fmt(float(row['kendall_grt_to_routed']) if row['kendall_grt_to_routed'] else None)} | {fmt(float(row['spearman_flute_clock_to_routed_clock_same_subset']) if row['spearman_flute_clock_to_routed_clock_same_subset'] else None)} |")
        gate_metrics = read_tsv(analysis / "grt_decoupled_gate_metrics.tsv")
        lines += ["", "## Combined GRT vs. decoupled component gate (same 68-candidate subset)", "", "The combined policy passes if GRT signal+clock delta is negative in all three workloads. The decoupled policy requires GRT signal delta ≤ 0 in every workload and GRT clock delta ≤ 0 in every workload; the separate signal-only and clock-only rows show each component's standalone effect. These are diagnostic results on this already-routed pool.", "", "| Policy | Actual target | Accepted | TP | FP | FN | TN | Precision | Recall |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for row in gate_metrics:
            lines.append(f"| {row['policy']} | {row['target']} | {row['accepted']} | {row['tp']} | {row['fp']} | {row['fn']} | {row['tn']} | {float(row['precision']):.3f} | {float(row['recall']):.3f} |")
        predictor_corr = read_tsv(analysis / "tier2_predictor_component_correlation.tsv")
        lines += ["", "## Predictor comparison on the Tier 2 eligible subset", "", "This compares fixed-baseline-VCD GRT signal/clock estimates with the existing placement score and HPWL, using the same 68 candidates and routed component labels.", "", "| Workload | Predictor | Target | Spearman ρ | Kendall τ-b |", "|---|---|---|---:|---:|"]
        for row in predictor_corr:
            lines.append(f"| {row['workload']} | {row['predictor']} | {row['target']} | {fmt(float(row['spearman']) if row['spearman'] else None)} | {fmt(float(row['kendall_tau_b']) if row['kendall_tau_b'] else None)} |")
    lines += ["", "## Interpretation constraints", "", "- GRT parasitics include estimated pin and wire capacitance after CTS, but do not include detailed-route coupling/topology. A better rank correlation here would justify a new pre-route experiment, not prove final-route accuracy.", "- Candidate-VCD results use traces generated from completed candidates. The fixed-baseline-VCD ablation avoids that candidate-specific activity, but still evaluates on this same holdout and uses post-CTS GRT capacitance.", "- Do not select γ or a margin from this holdout for deployment. Freeze the selected rule, then evaluate a new independent pool.", ""]
    report = analysis / "tier2_cross_row_diagnostic_report.md"
    report.write_text("\n".join(lines))
    print(f"TIER2_DIAGNOSTIC eligible={len(eligible)} tp={current_counts['tp']} fp={current_counts['fp']} fn={current_counts['fn']} report={report.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
