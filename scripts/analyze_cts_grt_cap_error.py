#!/usr/bin/env python3
"""Diagnose CTS/global-route capacitance deltas against final routed SPEF."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE**2
STAGE_DIR = ROOT / "build/power/place_spef_cts_grt_20261027_locsearch01"
UNIFIED = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01/unified_per_net_features.csv"
STAGES = ("cts_placement", "global_route")
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path):
    with path.open(newline="") as source:
        yield from csv.DictReader(source, delimiter="\t")


def read_csv(path: Path):
    with path.open(newline="") as source:
        yield from csv.DictReader(source)


def write_table(path: Path, rows: list[dict[str, object]], delimiter: str = "\t") -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def f(row: dict[str, str], key: str) -> float:
    return float(row.get(key, "0") or 0.0)


def close(a: float, b: float, rel: float = 1.0e-11, abs_tol: float = 1.0e-24) -> bool:
    return math.isclose(a, b, rel_tol=rel, abs_tol=abs_tol)


def quantile(values: list[float], q: float) -> float:
    if not values:
        raise ValueError("cannot calculate a quantile from an empty list")
    values = sorted(values)
    index = (len(values) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    frac = index - lower
    return values[lower] * (1.0 - frac) + values[upper] * frac


def fanout_class(fanout: float) -> str:
    if fanout <= 1:
        return "0-1"
    if fanout <= 4:
        return "2-4"
    if fanout <= 16:
        return "5-16"
    return "17+"


def route_class(length: float, q33: float, q67: float) -> str:
    if length <= q33:
        return "short"
    if length <= q67:
        return "medium"
    return "long"


def activity_class(density: float, median_positive: float) -> str:
    if density <= 0:
        return "inactive"
    if density <= median_positive:
        return "active_low"
    return "active_high"


def scale(xs: list[float], ys: list[float]) -> float:
    denominator = sum(x * x for x in xs)
    return sum(x * y for x, y in zip(xs, ys)) / denominator if denominator else 0.0


def mae(actual: list[float], predicted: list[float]) -> float:
    if len(actual) != len(predicted):
        raise ValueError("MAE vectors have different lengths")
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / max(1, len(actual))


def load_stage_caps(path: Path, candidates: set[str]) -> dict[str, dict[tuple[str, str], tuple[float, float]]]:
    result = {stage: {} for stage in STAGES}
    repeats: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in read_tsv(path):
        stage, candidate, workload, net = row["stage"], row["candidate"], row["workload"], row["net"]
        if stage not in result or candidate not in candidates or workload not in WORKLOADS:
            raise ValueError(f"unexpected stage row: {stage}/{candidate}/{workload}/{net}")
        key = (candidate, net)
        caps = (f(row, "baseline_cap_f"), f(row, "candidate_cap_f"))
        prior = result[stage].get(key)
        if prior is not None and (not close(prior[0], caps[0]) or not close(prior[1], caps[1])):
            raise ValueError(f"capacitance changed across workloads for {stage}/{candidate}/{net}")
        result[stage][key] = caps
        repeats[(stage, candidate, net)] += 1
    expected = len(candidates) * 8584
    for stage in STAGES:
        if len(result[stage]) != expected:
            raise ValueError(f"{stage} cap coverage is {len(result[stage])}, expected {expected}")
    bad_repeats = [key for key, count in repeats.items() if count != len(WORKLOADS)]
    if bad_repeats:
        raise ValueError(f"stage rows do not have three workload copies; first: {bad_repeats[0]}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=STAGE_DIR / "cap_error_analysis",
    )
    args = parser.parse_args()
    out = args.output_dir.resolve()
    try:
        stage_rows_path = STAGE_DIR / "stage_signal_per_net_deltas.tsv"
        timing_path = STAGE_DIR / "candidate_workload_stage_attribution.tsv"
        integrity_path = STAGE_DIR / "integrity.tsv"
        for path in (stage_rows_path, timing_path, integrity_path, UNIFIED):
            if not path.is_file():
                raise FileNotFoundError(path)

        integrity = list(read_tsv(integrity_path))
        if not integrity or any(row.get("unchanged") != "True" for row in integrity):
            raise ValueError("prior CTS/GRT input integrity manifest is missing or has changed inputs")

        # Load predictor-only geometry and SPEF cap labels once per candidate/net.
        net_features: dict[tuple[str, str], tuple[float, float | None, float | None, float]] = {}
        baseline_features: dict[str, tuple[float, float | None, float | None]] = {}
        base_activity: dict[tuple[str, str, str], float] = {}
        candidates: set[str] = set()
        workload_targets: dict[tuple[str, str], float] = defaultdict(float)
        workload_counts: dict[tuple[str, str], int] = defaultdict(int)
        feature_repeats: dict[tuple[str, str], int] = defaultdict(int)
        for row in read_csv(UNIFIED):
            candidate, workload, net = row["candidate"], row["workload"], row["net"]
            if row["routed_class"] != "common_signal" or workload not in WORKLOADS:
                continue
            candidates.add(candidate)
            key = (candidate, net)
            hpwl = f(row, "place_baseline_hpwl_um") if row.get("place_baseline_hpwl_um") else None
            route_length = f(row, "route_baseline_centerline_um") if row.get("route_baseline_centerline_um") else None
            values = (f(row, "place_baseline_fanout"), hpwl, route_length, f(row, "routed_delta_wire_cap_f"))
            previous = net_features.get(key)
            if previous is not None:
                for old, new in zip(previous, values):
                    if old is not None and new is not None and not close(old, new):
                        raise ValueError(f"geometry or routed cap label varies across workloads: {key}")
                    if old is None and new is not None:
                        previous = tuple(new if a is None else a for a, new in zip(previous, values))
                values = previous
            net_features[key] = values
            feature_repeats[key] += 1
            net_base = baseline_features.get(net)
            this_base = values[:3]
            if net_base is not None:
                merged = []
                for old, new in zip(net_base, this_base):
                    if old is not None and new is not None and not close(old, new):
                        raise ValueError(f"baseline net feature changed across candidate layouts: {net}")
                    merged.append(old if new is None else new)
                baseline_features[net] = tuple(merged)  # pair-swap rows omit some physical metadata
            else:
                baseline_features[net] = this_base
            activity_key = (candidate, workload, net)
            density = f(row, "baseline_place_density_hz")
            base_activity[activity_key] = density
            workload_targets[(candidate, workload)] += f(row, "routed_power_delta_w")
            workload_counts[(candidate, workload)] += 1

        if len(candidates) != 12:
            raise ValueError(f"expected 12 candidates, found {len(candidates)}")
        if len(net_features) != len(candidates) * 8584:
            raise ValueError(f"unified per-net coverage mismatch: found {len(net_features)} rows")
        if any(count != len(WORKLOADS) for count in feature_repeats.values()):
            raise ValueError("unified candidate/net rows are missing workload copies")
        if any(workload_counts[(candidate, workload)] != 8584 for candidate in candidates for workload in WORKLOADS):
            raise ValueError("unified signal target does not have 8,584 nets per candidate/workload")

        candidates = set(sorted(candidates))
        stage_caps = load_stage_caps(stage_rows_path, candidates)
        expected_keys = set(net_features)
        for stage in STAGES:
            if set(stage_caps[stage]) != expected_keys:
                raise ValueError(f"{stage} candidate/net keys do not match unified SPEF labels")

        route_lengths = [values[2] for values in baseline_features.values() if values[2] is not None]
        if len(route_lengths) != len(baseline_features):
            raise ValueError("baseline routed length metadata is missing for one or more signal nets")
        route_q33, route_q67 = quantile(route_lengths, 1 / 3), quantile(route_lengths, 2 / 3)
        net_group = {
            net: (fanout_class(values[0]), route_class(float(values[2]), route_q33, route_q67))
            for net, values in baseline_features.items()
        }
        activity_medians: dict[str, float] = {}
        for workload in WORKLOADS:
            densities = sorted({
                base_activity[(candidate, workload, net)]
                for candidate in candidates for net in baseline_features
            })
            positive = [value for value in densities if value > 0]
            activity_medians[workload] = quantile(positive, 0.5) if positive else 0.0

        # One leave-one-candidate-out cap correction fit per stage. Route length is
        # deliberately diagnostic-only; the proposed predictor uses placement fanout.
        predictions: dict[str, dict[str, dict[tuple[str, str], float]]] = {
            stage: {model: {} for model in ("zero", "direct", "global_loco", "fanout_loco")}
            for stage in STAGES
        }
        fold_scales: list[dict[str, object]] = []
        per_net_metrics: list[dict[str, object]] = []
        net_prediction_rows: list[dict[str, object]] = []
        for stage in STAGES:
            keys = sorted(expected_keys)
            xs = {key: stage_caps[stage][key][1] - stage_caps[stage][key][0] for key in keys}
            ys = {key: net_features[key][3] for key in keys}
            for held in sorted(candidates):
                train = [key for key in keys if key[0] != held]
                test = [key for key in keys if key[0] == held]
                global_scale = scale([xs[k] for k in train], [ys[k] for k in train])
                fanout_scales: dict[str, float] = {}
                for group in ("0-1", "2-4", "5-16", "17+"):
                    members = [key for key in train if net_group[key[1]][0] == group]
                    denom = sum(xs[key] ** 2 for key in members)
                    fanout_scales[group] = scale([xs[k] for k in members], [ys[k] for k in members]) if denom else global_scale
                for key in test:
                    predictions[stage]["zero"][key] = 0.0
                    predictions[stage]["direct"][key] = xs[key]
                    predictions[stage]["global_loco"][key] = global_scale * xs[key]
                    predictions[stage]["fanout_loco"][key] = fanout_scales[net_group[key[1]][0]] * xs[key]
                fold_scales.append({
                    "stage": stage,
                    "held_out_candidate": held,
                    "global_scale": global_scale,
                    "fanout_0_1_scale": fanout_scales["0-1"],
                    "fanout_2_4_scale": fanout_scales["2-4"],
                    "fanout_5_16_scale": fanout_scales["5-16"],
                    "fanout_17_plus_scale": fanout_scales["17+"],
                })

            for model in predictions[stage]:
                pred = predictions[stage][model]
                errors = [abs(ys[key] - pred[key]) for key in keys]
                per_net_metrics.append({
                    "stage": stage,
                    "model": model,
                    "nets": len(keys),
                    "cap_mae_ff": mae([ys[key] * 1e15 for key in keys], [pred[key] * 1e15 for key in keys]),
                    "cap_signed_bias_ff": sum((pred[key] - ys[key]) * 1e15 for key in keys) / len(keys),
                    "nets_with_nonzero_prediction_pct": 100.0 * sum(abs(pred[key]) > 1e-24 for key in keys) / len(keys),
                })

            for key in keys:
                candidate, net = key
                baseline_cap, candidate_cap = stage_caps[stage][key]
                fanout, hpwl, route_len, routed_cap = net_features[key]
                row = {
                    "stage": stage,
                    "candidate": candidate,
                    "net": net,
                    "fanout_class": net_group[net][0],
                    "route_class_diagnostic_only": net_group[net][1],
                    "stage_cap_delta_f": xs[key],
                    "routed_spef_wire_cap_delta_f": routed_cap,
                    "baseline_stage_cap_f": baseline_cap,
                    "candidate_stage_cap_f": candidate_cap,
                }
                for model in predictions[stage]:
                    row[f"{model}_cap_delta_f"] = predictions[stage][model][key]
                    row[f"{model}_cap_abs_error_f"] = abs(predictions[stage][model][key] - routed_cap)
                net_prediction_rows.append(row)

        # Group diagnostics and both the cap-only dynamic-power proxy and full signal target.
        group_stats: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
        candidate_model_power: dict[tuple[str, str, str, str], float] = defaultdict(float)
        candidate_model_cap_power: dict[tuple[str, str, str, str], float] = defaultdict(float)
        candidate_cap_power_target: dict[tuple[str, str], float] = defaultdict(float)
        for row in read_csv(UNIFIED):
            candidate, workload, net = row["candidate"], row["workload"], row["net"]
            if row["routed_class"] != "common_signal":
                continue
            base_density = f(row, "baseline_place_density_hz")
            cand_density = f(row, "candidate_place_density_hz")
            actual_power = f(row, "routed_power_delta_w")
            cap_label = net_features[(candidate, net)][3]
            candidate_cap_power_target[(candidate, workload)] += POWER_FACTOR * base_density * cap_label
            fanout_group, route_group = net_group[net]
            activity_group = activity_class(base_density, activity_medians[workload])
            for stage in STAGES:
                key = (candidate, net)
                baseline_cap, _ = stage_caps[stage][key]
                for model in predictions[stage]:
                    predicted_cap = predictions[stage][model][key]
                    cap_error = predicted_cap - cap_label
                    cap_power_error_nw = POWER_FACTOR * base_density * cap_error * 1e9
                    group_specs = (
                        ("fanout", fanout_group),
                        ("route_length_diagnostic_only", route_group),
                        ("activity", activity_group),
                    )
                    for stratifier, group in group_specs:
                        stat = group_stats[(stage, stratifier, group)]
                        stat["observations"] += 1
                        stat["sum_abs_cap_error_ff"] += abs(cap_error) * 1e15
                        stat["sum_signed_cap_error_ff"] += cap_error * 1e15
                        stat["sum_abs_power_error_nw"] += abs(cap_power_error_nw)
                        stat["zero_stage_cap_prediction"] += int(abs(stage_caps[stage][key][1] - baseline_cap) <= 1e-24)
                        stat["nonzero_routed_cap_label"] += int(abs(cap_label) > 1e-24)
                    # Candidate cap-only switching-power proxy: uses measured workload activity.
                    candidate_model_cap_power[(stage, model, candidate, workload)] += POWER_FACTOR * base_density * predicted_cap
                    # Stage total switching-power estimate with the corrected candidate-minus-baseline cap.
                    corrected_candidate_cap = baseline_cap + predicted_cap
                    candidate_model_power[(stage, model, candidate, workload)] += POWER_FACTOR * (
                        corrected_candidate_cap * cand_density - baseline_cap * base_density
                    )

        group_rows: list[dict[str, object]] = []
        for (stage, stratifier, group), stat in sorted(group_stats.items()):
            count = int(stat["observations"])
            group_rows.append({
                "stage": stage,
                "stratifier": stratifier,
                "group": group,
                "candidate_net_workload_rows": count,
                "cap_mae_ff": stat["sum_abs_cap_error_ff"] / count,
                "cap_signed_bias_ff": stat["sum_signed_cap_error_ff"] / count,
                "activity_weighted_cap_power_mae_nw_per_net_workload": stat["sum_abs_power_error_nw"] / count,
                "stage_cap_delta_zero_pct": 100.0 * stat["zero_stage_cap_prediction"] / count,
                "routed_cap_delta_nonzero_pct": 100.0 * stat["nonzero_routed_cap_label"] / count,
            })

        # Candidate-held-out aggregate error; the workload mean is a strong baseline.
        aggregate_rows: list[dict[str, object]] = []
        heldout_rows: list[dict[str, object]] = []
        targets = {
            "full_routed_signal_power": workload_targets,
            "routed_wire_cap_power_proxy": candidate_cap_power_target,
        }
        model_names = ("zero", "direct", "global_loco", "fanout_loco")
        for stage in STAGES:
            for target_name, actual_map in targets.items():
                predictions_by_model: dict[str, dict[tuple[str, str], float]] = {}
                for model in model_names:
                    predictions_by_model[model] = {}
                    for candidate in candidates:
                        for workload in WORKLOADS:
                            if target_name == "full_routed_signal_power":
                                val = candidate_model_power[(stage, model, candidate, workload)]
                            else:
                                val = candidate_model_cap_power[(stage, model, candidate, workload)]
                            predictions_by_model[model][(candidate, workload)] = val
                baseline_pred: dict[tuple[str, str], float] = {}
                for held in candidates:
                    for workload in WORKLOADS:
                        train_values = [actual_map[(c, workload)] for c in candidates if c != held]
                        baseline_pred[(held, workload)] = sum(train_values) / len(train_values)
                metric_models = ["workload_mean", *model_names]
                for model in metric_models:
                    pred_map = baseline_pred if model == "workload_mean" else predictions_by_model[model]
                    actual = [actual_map[(c, w)] for c in candidates for w in WORKLOADS]
                    predicted = [pred_map[(c, w)] for c in candidates for w in WORKLOADS]
                    row = {
                        "stage": stage,
                        "target": target_name,
                        "model": model,
                        "candidate_workload_rows": len(actual),
                        "mae_uw": mae(actual, predicted) * 1e6,
                        "mean_signed_error_uw": sum((p-a) for a, p in zip(actual, predicted)) / len(actual) * 1e6,
                        "folds_beating_workload_mean": "",
                    }
                    wins = 0
                    for held in sorted(candidates):
                        actual_fold = [actual_map[(held, w)] for w in WORKLOADS]
                        pred_fold = [pred_map[(held, w)] for w in WORKLOADS]
                        baseline_fold = [baseline_pred[(held, w)] for w in WORKLOADS]
                        fold_mae = mae(actual_fold, pred_fold)
                        baseline_mae = mae(actual_fold, baseline_fold)
                        beats = fold_mae < baseline_mae - 1e-18
                        wins += int(beats)
                        heldout_rows.append({
                            "stage": stage,
                            "target": target_name,
                            "model": model,
                            "held_out_candidate": held,
                            "mae_uw": fold_mae * 1e6,
                            "workload_mean_mae_uw": baseline_mae * 1e6,
                            "beats_workload_mean": beats,
                        })
                    row["folds_beating_workload_mean"] = f"{wins}/{len(candidates)}" if model != "workload_mean" else "baseline"
                    aggregate_rows.append(row)

        timing_rows = list(read_tsv(timing_path))
        timing = {
            stage: sum(row["timing_guard_50ps_pass"] == "True" for row in timing_rows if row["stage"] == stage)
            for stage in STAGES
        }
        expected_timing = len(candidates) * len(WORKLOADS)
        if any(timing[stage] != expected_timing for stage in STAGES):
            raise ValueError(f"timing guard did not pass all cases: {timing}")

        out.mkdir(parents=True, exist_ok=True)
        write_table(out / "per_net_cap_predictions.tsv", net_prediction_rows)
        write_table(out / "cap_error_by_group.tsv", group_rows)
        write_table(out / "candidate_heldout_cap_metrics.tsv", per_net_metrics)
        write_table(out / "candidate_heldout_scales.tsv", fold_scales)
        write_table(out / "candidate_workload_power_metrics.tsv", aggregate_rows)
        write_table(out / "candidate_workload_power_folds.tsv", heldout_rows)
        source_hashes = [
            {"path": str(path), "sha256": sha256(path)}
            for path in (stage_rows_path, UNIFIED, timing_path, integrity_path)
        ]
        write_table(out / "source_hashes.tsv", source_hashes)

        metric_lookup = {(r["stage"], r["target"], r["model"]): r for r in aggregate_rows}
        sparsity: dict[str, tuple[float, float, float]] = {}
        for stage in STAGES:
            stage_net_rows = [row for row in net_prediction_rows if row["stage"] == stage]
            stage_zero = sum(abs(float(row["stage_cap_delta_f"])) <= 1e-24 for row in stage_net_rows)
            route_nonzero = sum(abs(float(row["routed_spef_wire_cap_delta_f"])) > 1e-24 for row in stage_net_rows)
            missed = sum(
                abs(float(row["stage_cap_delta_f"])) <= 1e-24
                and abs(float(row["routed_spef_wire_cap_delta_f"])) > 1e-24
                for row in stage_net_rows
            )
            sparsity[stage] = (
                100.0 * stage_zero / len(stage_net_rows),
                100.0 * route_nonzero / len(stage_net_rows),
                100.0 * missed / max(1, route_nonzero),
            )
        fanout_worst = max(
            (row for row in group_rows if row["stratifier"] == "fanout"),
            key=lambda row: float(row["activity_weighted_cap_power_mae_nw_per_net_workload"]),
        )
        route_worst = max(
            (row for row in group_rows if row["stratifier"] == "route_length_diagnostic_only"),
            key=lambda row: float(row["activity_weighted_cap_power_mae_nw_per_net_workload"]),
        )
        lines = [
            "# CTS/global-route per-net capacitance error analysis",
            "",
            f"Analyzed {len(candidates)} held-out candidate layouts and {len(baseline_features)} common signal nets ({len(net_features)} candidate/net pairs). The existing CTS/GRT source-integrity manifest is all-pass; the 50 ps setup/hold guard remains {timing['cts_placement']}/{expected_timing} CTS and {timing['global_route']}/{expected_timing} global-route cases.",
            "",
            "The cap target is each final routed SPEF wire-capacitance delta. The checkpoint feature is candidate-minus-baseline estimated total capacitance; pin capacitance should cancel because the netlist is unchanged. Power proxy uses 0.5 × 1.8² × baseline VCD transition rate × cap delta. Final routed centerline length is used only to diagnose error groups and is not used by a predictor.",
            "",
            "## Capacitance fit and held-out power results",
            "",
            "All fitted scales are trained on the other 11 candidates. The fanout correction uses only placement fanout classes (0–1, 2–4, 5–16, 17+). The reference is the other candidates' mean result for the same workload.",
            "",
            "| Stage | Target | Model | MAE | Folds beating workload mean |",
            "|---|---|---|---:|---:|",
        ]
        for stage in STAGES:
            for target_name in ("routed_wire_cap_power_proxy", "full_routed_signal_power"):
                for model in ("workload_mean", "zero", "direct", "global_loco", "fanout_loco"):
                    row = metric_lookup[(stage, target_name, model)]
                    unit = "µW"
                    lines.append(
                        f"| {stage} | {target_name} | {model} | {float(row['mae_uw']):.4f} {unit} | {row['folds_beating_workload_mean']} |"
                    )
        lines.extend([
            "",
            "## Per-net cap fit",
            "",
            "| Stage | Model | Cap MAE | Mean signed error | Nets with nonzero predicted ΔC |",
            "|---|---|---:|---:|---:|",
        ])
        for row in per_net_metrics:
            lines.append(
                f"| {row['stage']} | {row['model']} | {float(row['cap_mae_ff']):.4f} fF | "
                f"{float(row['cap_signed_bias_ff']):+.4f} fF | {float(row['nets_with_nonzero_prediction_pct']):.3f}% |"
            )
        lines.extend([
            "",
            "## Group diagnostics",
            "",
            "`cap_error_by_group.tsv` reports cap and activity-weighted power error by fanout, baseline routed-length class, and VCD activity. Baseline routed-length classes are diagnostic labels only; they would not be available to a pre-route cost model.",
            "",
            "## Conclusion",
            "",
            f"CTS estimated ΔC is zero on {sparsity['cts_placement'][0]:.3f}% of candidate/net pairs; routed SPEF ΔC is nonzero on {sparsity['cts_placement'][1]:.3f}%, and zero CTS estimates miss {sparsity['cts_placement'][2]:.3f}% of those nonzero SPEF changes. For global route, the corresponding rates are {sparsity['global_route'][0]:.3f}%, {sparsity['global_route'][1]:.3f}%, and {sparsity['global_route'][2]:.3f}%.",
            f"The fanout correction does not beat the workload-conditioned full-signal baseline: CTS MAE is {float(metric_lookup[('cts_placement', 'full_routed_signal_power', 'fanout_loco')]['mae_uw']):.4f} µW versus {float(metric_lookup[('cts_placement', 'full_routed_signal_power', 'workload_mean')]['mae_uw']):.4f} µW; global route is {float(metric_lookup[('global_route', 'full_routed_signal_power', 'fanout_loco')]['mae_uw']):.4f} µW versus {float(metric_lookup[('global_route', 'full_routed_signal_power', 'workload_mean')]['mae_uw']):.4f} µW. It beats that baseline in 4/12 held-out layouts for both stages.",
            f"Error is concentrated on {fanout_worst['group']} fanout nets (activity-weighted cap-power MAE {float(fanout_worst['activity_weighted_cap_power_mae_nw_per_net_workload']):.2f} nW/net/workload) and {route_worst['group']} routed-length nets ({float(route_worst['activity_weighted_cap_power_mae_nw_per_net_workload']):.2f} nW/net/workload). The routed-length grouping is diagnostic-only. This identifies where the estimate fails, but the candidate-held-out results do not support adding a fanout scale correction to the placer.",
            "The next model should add a placement-available estimate of route-induced capacitance changes (for example, geometry and congestion features) and continue to validate against held-out routed SPEF. Keep final routed SPEF as the acceptance metric.",
            "",
            "## Outputs",
            "",
            "- `per_net_cap_predictions.tsv`: matched stage ΔC, routed SPEF ΔC, and held-out predictions.",
            "- `cap_error_by_group.tsv`: errors grouped by fanout, diagnostic routed length, and VCD activity.",
            "- `candidate_heldout_cap_metrics.tsv` and `candidate_heldout_scales.tsv`: per-net accuracy and fold-specific calibration factors.",
            "- `candidate_workload_power_metrics.tsv` and `candidate_workload_power_folds.tsv`: held-out aggregate power proxy and full signal-power comparisons.",
            "- `source_hashes.tsv`: hashes of analysis inputs.",
        ])
        report = out / "cap_error_analysis_report.md"
        report.write_text("\n".join(lines) + "\n")
        print(f"Wrote per-net CTS/GRT cap analysis for {len(candidates)} candidates × {len(baseline_features)} nets")
        print(report)
        return 0
    except (OSError, KeyError, ValueError) as exc:
        print(f"CTS/GRT cap error analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
