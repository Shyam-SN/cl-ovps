#!/usr/bin/env python3
"""Merge the 10-move calibration and two routed pair swaps for held-out analysis."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
EPSILON = 1.0e-15
BASELINE_PLACE_SHA = "2c3de3ddff887a6ef3e60de834eda9c3c16bd5b81675eef66b623cf5c762db0f"
EXPANSION_ANALYSIS = ROOT / "build/power/place_spef_expansion_analysis_20261027_calset01a"
PAIR_DIR = ROOT / "build/power/vcd_swap_audit_20261027_locsearch01/pair_calibration"
OLD_CANDIDATES = {
    "place538:+460": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place538_m460",
    "_12131_:-460": ROOT / "build/power/place_spef_calibration_20261027_002801__12131__m460",
    "place540:-460": ROOT / "build/power/place_spef_calibration_20261027_002801_place540_m460",
    "place544:+460": ROOT / "build/power/place_spef_calibration_20261027_002801_place544_p460",
    "_12131_:+5520": ROOT / "build/power/place_spef_calibration_20261027_calset01a__12131__p5520",
    "place538:-460": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place538_m460",
    "place570:+5060": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place570_p5060",
    "place574:-4140": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place574_m4140",
    "place643:-460": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place643_m460",
    "place654:+460": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place654_p460",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def write_csv(path: Path, rows: list[dict[str, object]], delimiter: str = ",") -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def number(row: dict[str, str] | dict[str, object], key: str) -> float:
    return float(row.get(key, 0) or 0.0)


def check_manifest(path: Path) -> dict[str, str]:
    values = dict(line.split("\t", 1) for line in path.read_text().splitlines() if "\t" in line)
    for before, after in (
        ("baseline_place_odb_sha256", "baseline_place_odb_sha256_after"),
        ("candidate_place_odb_sha256", "candidate_place_odb_sha256_after"),
        ("placement_sdc_sha256", "placement_sdc_sha256_after"),
        ("routed_baseline_spef_sha256_before", "routed_baseline_spef_sha256_after"),
        ("routed_candidate_spef_sha256_before", "routed_candidate_spef_sha256_after"),
    ):
        if not values.get(before) or values[before] != values.get(after):
            raise ValueError(f"integrity manifest mismatch for {before} in {path}")
    return values


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    if xx == 0 or yy == 0:
        return None
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for i in range(start, end):
            result[order[i]] = rank
        start = end
    return result


def mae(actual: list[float], predicted: list[float]) -> float:
    return sum(abs(y - p) for y, p in zip(actual, predicted)) / max(1, len(actual))


def fit_scale(xs: list[float], ys: list[float]) -> float:
    denominator = sum(x * x for x in xs)
    return sum(x * y for x, y in zip(xs, ys)) / denominator if denominator else 0.0


def summarize_per_net(rows: list[dict[str, object]], feature_map: dict[str, object]) -> list[dict[str, object]]:
    labels = sorted({str(row["candidate"]) for row in rows})
    actual = [number(row, "routed_power_delta_w") for row in rows]
    output = []
    for name, get_feature in feature_map.items():
        xs = [float(get_feature(row)) for row in rows]
        prediction = [0.0] * len(rows)
        scales = {}
        for held in labels:
            train = [i for i, row in enumerate(rows) if str(row["candidate"]) != held]
            test = [i for i, row in enumerate(rows) if str(row["candidate"]) == held]
            scale = fit_scale([xs[i] for i in train], [actual[i] for i in train])
            scales[held] = scale
            for i in test:
                prediction[i] = scale * xs[i]
        r, rho = pearson(xs, actual), pearson(ranks(xs), ranks(actual))
        output.append({
            "feature": name,
            "rows": len(rows),
            "zero_mae_w": mae(actual, [0.0] * len(actual)),
            "raw_mae_w": mae(actual, xs) if name in {"placement_power", "placement_wire_proxy"} else "",
            "loco_scaled_mae_w": mae(actual, prediction),
            "pearson_r": r if r is not None else "",
            "spearman_rho": rho if rho is not None else "",
            "heldout_scales": ";".join(f"{label}={scales[label]:.6g}" for label in labels),
        })
    return output


def aggregate_rows(
    all_rows: list[dict[str, object]],
    old_candidate_dirs: dict[str, Path],
    pair_dir: Path,
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in all_rows:
        grouped[(str(row["candidate"]), str(row["workload"]))].append(row)
    result = []
    for candidate in sorted({str(row["candidate"]) for row in all_rows}):
        for workload in WORKLOADS:
            rows = grouped[(candidate, workload)]
            if candidate in old_candidate_dirs:
                route_path = old_candidate_dirs[candidate] / f"{workload}.placement_vs_routed.per_net.csv"
                route_rows = read_csv(route_path)
                clock = sum(number(row, "routed_power_delta_w") for row in route_rows if row["routed_class"] == "postcts_clock")
                other = sum(
                    number(row, "routed_power_delta_w") for row in route_rows
                    if row["routed_class"] == "not_common_to_placement"
                    and row["routed_activity_origin"] != "clock"
                )
                route_total = sum(number(row, "routed_power_delta_w") for row in route_rows)
            else:
                route_dir = ROOT / f"build/power/vcdswap_20261027_locsearch01_{candidate}/net_attribution"
                route_rows = read_csv(route_dir / f"{workload}.per_net_delta.csv")
                clock = sum(
                    number(row, "delta_switching_power_w") for row in route_rows
                    if row["baseline_activity_origin"] == "clock" or row["net"].startswith("clknet_")
                )
                signal_nets = {str(row["net"]) for row in rows}
                other = sum(
                    number(row, "delta_switching_power_w") for row in route_rows
                    if row["net"] not in signal_nets
                    and row["baseline_activity_origin"] != "clock"
                    and not row["net"].startswith("clknet_")
                )
                route_total = sum(number(row, "delta_switching_power_w") for row in route_rows)
            place_signal = sum(number(row, "place_predicted_power_delta_w") for row in rows)
            route_signal = sum(number(row, "routed_power_delta_w") for row in rows)
            route_wire_cap = sum(number(row, "routed_delta_wire_cap_f") for row in rows)
            result.append({
                "candidate": candidate,
                "workload": workload,
                "placement_signal_prediction_w": place_signal,
                "routed_signal_delta_w": route_signal,
                "routed_clock_delta_w": clock,
                "routed_other_nonclock_delta_w": other,
                "routed_total_per_net_delta_w": route_total,
                "signal_routed_wire_cap_delta_f": route_wire_cap,
                "placement_abs_hpwl_sum_um": sum(number(row, "place_abs_delta_hpwl_um") for row in rows),
                "placement_hpwl_delta_sum_um": sum(number(row, "place_delta_hpwl_um") for row in rows),
                "placement_activity_weighted_hpwl_delta": sum(
                    number(row, "baseline_place_density_hz") * number(row, "place_delta_hpwl_um")
                    for row in rows
                ),
                "placement_activity_weighted_abs_hpwl_delta": sum(
                    number(row, "baseline_place_density_hz") * number(row, "place_abs_delta_hpwl_um")
                    for row in rows
                ),
                "route_centerline_delta_sum_um": sum(number(row, "route_delta_centerline_um") for row in rows),
                "route_via_delta_sum": sum(number(row, "route_delta_vias") for row in rows),
            })
    return result


def candidate_loco(
    summaries: list[dict[str, object]], features: dict[str, object]
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    output = []
    predictions = []
    for feature_name, get_feature in features.items():
        xs = [float(get_feature(row)) for row in summaries]
        for target in ("routed_signal_delta_w", "routed_total_per_net_delta_w"):
            ys = [number(row, target) for row in summaries]
            pred = [0.0] * len(summaries)
            mean_pred = [0.0] * len(summaries)
            workload_mean_pred = [0.0] * len(summaries)
            scales_by_candidate: dict[str, float] = {}
            labels = sorted({str(row["candidate"]) for row in summaries})
            for held in labels:
                train = [i for i, row in enumerate(summaries) if str(row["candidate"]) != held]
                test = [i for i, row in enumerate(summaries) if str(row["candidate"]) == held]
                scale = fit_scale([xs[i] for i in train], [ys[i] for i in train])
                train_mean = sum(ys[i] for i in train) / len(train)
                scales_by_candidate[held] = scale
                for i in test:
                    pred[i] = scale * xs[i]
                    mean_pred[i] = train_mean
                    same_workload = [j for j in train if summaries[j]["workload"] == summaries[i]["workload"]]
                    workload_mean_pred[i] = sum(ys[j] for j in same_workload) / len(same_workload)
            is_absolute = "abs_hpwl" in feature_name
            direction_cases = sum(abs(p) > EPSILON and abs(y) > EPSILON for p, y in zip(pred, ys))
            output.append({
                "feature": feature_name,
                "target": target,
                "rows": len(summaries),
                "zero_mae_w": mae(ys, [0.0] * len(ys)),
                "fold_mean_mae_w": mae(ys, mean_pred),
                "fold_workload_mean_mae_w": mae(ys, workload_mean_pred),
                "raw_mae_w": mae(ys, xs) if feature_name in {"placement_power", "placement_wire_proxy"} else "",
                "loco_scaled_mae_w": mae(ys, pred),
                "pearson_r": pearson(xs, ys) if pearson(xs, ys) is not None else "",
                "direction_matches": "" if is_absolute else sum(p * y > 0 for p, y in zip(pred, ys) if abs(p) > EPSILON and abs(y) > EPSILON),
                "direction_cases": "" if is_absolute else direction_cases,
            })
            for index, row in enumerate(summaries):
                predictions.append({
                    "feature": feature_name,
                    "target": target,
                    "candidate": row["candidate"],
                    "workload": row["workload"],
                    "actual_delta_w": ys[index],
                    "zero_prediction_w": 0.0,
                    "fold_mean_prediction_w": mean_pred[index],
                    "fold_workload_mean_prediction_w": workload_mean_pred[index],
                    "feature_value": xs[index],
                    "fold_scale": scales_by_candidate[str(row["candidate"])],
                    "loco_feature_prediction_w": pred[index],
                    "zero_abs_error_w": abs(ys[index]),
                    "fold_mean_abs_error_w": abs(ys[index] - mean_pred[index]),
                    "fold_workload_mean_abs_error_w": abs(ys[index] - workload_mean_pred[index]),
                    "loco_feature_abs_error_w": abs(ys[index] - pred[index]),
                })
    folds: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in predictions:
        folds[(str(row["feature"]), str(row["target"]), str(row["candidate"]))].append(row)
    fold_summary = []
    for (feature, target, candidate), rows in sorted(folds.items()):
        count = len(rows)
        zero_error = sum(number(row, "zero_abs_error_w") for row in rows) / count
        mean_error = sum(number(row, "fold_mean_abs_error_w") for row in rows) / count
        workload_mean_error = sum(number(row, "fold_workload_mean_abs_error_w") for row in rows) / count
        feature_error = sum(number(row, "loco_feature_abs_error_w") for row in rows) / count
        fold_summary.append({
            "feature": feature,
            "target": target,
            "candidate": candidate,
            "workloads": count,
            "zero_mae_w": zero_error,
            "fold_mean_mae_w": mean_error,
            "fold_workload_mean_mae_w": workload_mean_error,
            "loco_feature_mae_w": feature_error,
            "feature_minus_fold_mean_mae_w": feature_error - mean_error,
            "feature_minus_workload_mean_mae_w": feature_error - workload_mean_error,
            "feature_beats_fold_mean": feature_error < mean_error,
            "feature_beats_fold_workload_mean": feature_error < workload_mean_error,
        })
    return output, predictions, fold_summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path,
        default=ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01",
    )
    args = parser.parse_args()
    try:
        old_rows = read_csv(EXPANSION_ANALYSIS / "pooled_per_net_features.csv")
        pair_rows = read_csv(PAIR_DIR / "analysis/matched_per_net_features.csv")
        old_labels = set(OLD_CANDIDATES)
        found_old = {row["candidate"] for row in old_rows}
        if found_old != old_labels:
            raise ValueError(f"expanded calibration candidate mismatch: expected {sorted(old_labels)}, found {sorted(found_old)}")
        if len(old_rows) != 10 * len(WORKLOADS) * 8584:
            raise ValueError(f"expected 257,520 expanded signal rows, found {len(old_rows)}")
        pair_only = [row for row in pair_rows if row["candidate"] in {"swap44", "swap52"}]
        if len(pair_only) != 2 * len(WORKLOADS) * 8584:
            raise ValueError(f"expected 51,504 non-duplicate pair-swap rows, found {len(pair_only)}")

        # Recheck the ten existing source manifests and their shared placement baseline/SDC/SPEF.
        manifests = {label: check_manifest(directory / "manifest.tsv") for label, directory in OLD_CANDIDATES.items()}
        for key in (
            "baseline_place_odb_sha256",
            "placement_sdc_sha256",
            "routed_baseline_spef_sha256_before",
        ):
            values = {row[key] for row in manifests.values()}
            if len(values) != 1:
                raise ValueError(f"the ten prior candidates do not share {key}")
        if next(iter({row["baseline_place_odb_sha256"] for row in manifests.values()})) != BASELINE_PLACE_SHA:
            raise ValueError("the ten-candidate calibration uses a different baseline placement ODB")

        before_hashes = (PAIR_DIR / "input_hashes_before.tsv").read_bytes()
        after_hashes = (PAIR_DIR / "input_hashes_after.tsv").read_bytes()
        if before_hashes != after_hashes:
            raise ValueError("pair-swap calibration source hashes differ before and after analysis")
        pair_hashes = {
            path: digest
            for digest, path in (line.split("\t", 1) for line in before_hashes.decode().splitlines())
        }
        baseline_place_path = str(ROOT / "build/orfs/results/sky130hd/picorv32_baseline/baseline/3_5_place_dp.odb")
        baseline_spef_path = str(ROOT / "build/orfs/results/sky130hd/picorv32_baseline/baseline/6_final.spef")
        if pair_hashes.get(baseline_place_path) != BASELINE_PLACE_SHA:
            raise ValueError("pair-swap baseline placement checkpoint differs from the ten-move set")
        if pair_hashes.get(baseline_spef_path) != next(iter({row["routed_baseline_spef_sha256_before"] for row in manifests.values()})):
            raise ValueError("pair-swap routed baseline SPEF differs from the ten-move set")

        rows: list[dict[str, object]] = [dict(row) for row in old_rows]
        rows.extend(dict(row) for row in pair_only)
        if len(rows) != 12 * len(WORKLOADS) * 8584:
            raise ValueError(f"expected 309,024 unified rows, found {len(rows)}")
        coverage = defaultdict(int)
        seen = set()
        for row in rows:
            key = (row["candidate"], row["workload"], row["net"])
            if key in seen:
                raise ValueError(f"duplicate candidate/workload/net key: {key}")
            seen.add(key)
            coverage[(row["candidate"], row["workload"])] += 1
        if len(coverage) != 12 * len(WORKLOADS) or any(value != 8584 for value in coverage.values()):
            raise ValueError("unified set does not have complete 8,584-net coverage for every candidate/workload")

        feature_map = {
            "placement_power": lambda row: number(row, "place_predicted_power_delta_w"),
            "placement_wire_proxy": lambda row: 0.5 * VOLTAGE**2 * number(row, "baseline_place_density_hz") * number(row, "delta_place_wire_cap_f"),
            "placement_load_cap_delta": lambda row: number(row, "delta_place_load_cap_f"),
            "placement_hpwl_delta_um": lambda row: number(row, "place_delta_hpwl_um"),
            "placement_abs_hpwl_delta_um": lambda row: number(row, "place_abs_delta_hpwl_um"),
            "routed_centerline_delta_um (post-route)": lambda row: number(row, "route_delta_centerline_um"),
            "routed_via_delta (post-route)": lambda row: number(row, "route_delta_vias"),
            "routed_wire_proxy (post-route oracle)": lambda row: 0.5 * VOLTAGE**2 * number(row, "baseline_place_density_hz") * number(row, "routed_delta_wire_cap_f"),
        }
        per_net_metrics = summarize_per_net(rows, feature_map)
        summaries = aggregate_rows(rows, OLD_CANDIDATES, PAIR_DIR)
        aggregate_features = {
            "placement_power": lambda row: number(row, "placement_signal_prediction_w"),
            "placement_abs_hpwl_sum": lambda row: number(row, "placement_abs_hpwl_sum_um"),
            "placement_hpwl_delta_sum": lambda row: number(row, "placement_hpwl_delta_sum_um"),
            "activity_weighted_hpwl_delta": lambda row: number(row, "placement_activity_weighted_hpwl_delta"),
            "activity_weighted_abs_hpwl_delta": lambda row: number(row, "placement_activity_weighted_abs_hpwl_delta"),
            "routed_centerline_sum (post-route)": lambda row: number(row, "route_centerline_delta_sum_um"),
            "routed_via_sum (post-route)": lambda row: number(row, "route_via_delta_sum"),
        }
        candidate_metrics, candidate_predictions, candidate_folds = candidate_loco(summaries, aggregate_features)

        out = args.output_dir.resolve()
        write_csv(out / "unified_per_net_features.csv", rows)
        write_csv(out / "candidate_workload_attribution.tsv", summaries, "\t")
        write_csv(out / "per_net_loco_metrics.tsv", per_net_metrics, "\t")
        write_csv(out / "candidate_loco_metrics.tsv", candidate_metrics, "\t")
        write_csv(out / "candidate_loco_predictions.tsv", candidate_predictions, "\t")
        write_csv(out / "candidate_loco_fold_summary.tsv", candidate_folds, "\t")

        changed_pred = sum(abs(number(row, "place_predicted_power_delta_w")) > EPSILON for row in rows)
        changed_route = sum(abs(number(row, "routed_power_delta_w")) > EPSILON for row in rows)
        changed_route_zero_place = sum(
            abs(number(row, "routed_power_delta_w")) > EPSILON
            and abs(number(row, "place_predicted_power_delta_w")) <= EPSILON
            for row in rows
        )
        place_metric = next(row for row in per_net_metrics if row["feature"] == "placement_power")
        candidate_signal = next(row for row in candidate_metrics if row["feature"] == "placement_power" and row["target"] == "routed_signal_delta_w")
        candidate_total = next(row for row in candidate_metrics if row["feature"] == "placement_power" and row["target"] == "routed_total_per_net_delta_w")
        direction_match = candidate_signal["direction_matches"]
        direction_cases = candidate_signal["direction_cases"]

        lines = [
            "# Unified VCD placement to routed SPEF calibration",
            "",
            "## Coverage and integrity",
            "",
            "Combined the 10 previously routed single-cell moves with the two new pair swaps. Duplicate single-cell candidates were excluded. The dataset contains 12 candidates × 3 workloads × 8,584 common signal nets = 309,024 rows. Every candidate/workload has full net coverage with no duplicate candidate/workload/net keys.",
            "",
            "All ten prior calibration manifests passed before/after checks and share the same baseline placement ODB, placement SDC, and routed baseline SPEF. The pair-swap before/after source-hash snapshots match and identify the same baseline placement ODB and routed baseline SPEF. Clock-tree deltas are aggregated separately from the common signal-net rows.",
            "",
            "## Per-net held-out comparison",
            "",
            "MAE is nW per common signal net/workload row. LOCO means leave one complete candidate out: all three workloads and all nets for that candidate are held out together. Route geometry features are diagnostics only because a pre-route placer cannot observe them. Activity-weighted HPWL is the sum, per workload, of baseline VCD transition rate multiplied by signed or absolute placement HPWL change for each common signal net.",
            "",
            "| Feature | Zero MAE | Raw MAE | LOCO scaled MAE | Pearson r | Spearman ρ |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for metric in per_net_metrics:
            raw = "—" if metric["raw_mae_w"] == "" else f"{float(metric['raw_mae_w'])*1e9:.4f}"
            r = "—" if metric["pearson_r"] == "" else f"{float(metric['pearson_r']):+.4f}"
            rho = "—" if metric["spearman_rho"] == "" else f"{float(metric['spearman_rho']):+.4f}"
            display_feature = str(metric["feature"]).replace(" (post-route oracle)", "").replace(" (post-route)", "")
            suffix = " (post-route diagnostic)" if "post-route" in str(metric["feature"]) else ""
            lines.append(
                f"| {display_feature}{suffix} | {float(metric['zero_mae_w'])*1e9:.4f} | {raw} | "
                f"{float(metric['loco_scaled_mae_w'])*1e9:.4f} | {r} | {rho} |"
            )
        lines.extend([
            "",
            "## Candidate/workload held-out comparison",
            "",
            "The aggregate table has 36 rows (12 candidates × three workloads). Placement power is a signal-only pre-route estimate. Full routed power includes post-CTS clocks.",
            "",
            "| Feature | Target | Zero MAE | Global fold mean | Workload fold mean | Raw MAE | LOCO scaled MAE | Folds beating workload mean | LOCO direction matches |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ])
        for metric in candidate_metrics:
            if metric["feature"] not in {
                "placement_power", "placement_abs_hpwl_sum", "placement_hpwl_delta_sum",
                "activity_weighted_hpwl_delta", "activity_weighted_abs_hpwl_delta",
            }:
                continue
            raw = "—" if metric["raw_mae_w"] == "" else f"{float(metric['raw_mae_w'])*1e6:.3f} µW"
            fold_mean = f"{float(metric['fold_mean_mae_w'])*1e6:.3f} µW"
            workload_fold_mean = f"{float(metric['fold_workload_mean_mae_w'])*1e6:.3f} µW"
            target = "signal subtotal" if metric["target"] == "routed_signal_delta_w" else "full total incl. clock"
            direction = "—" if metric["direction_matches"] == "" else f"{metric['direction_matches']}/{metric['direction_cases']}"
            feature_folds = [
                row for row in candidate_folds
                if row["feature"] == metric["feature"] and row["target"] == metric["target"]
            ]
            better_folds = sum(bool(row["feature_beats_fold_workload_mean"]) for row in feature_folds)
            lines.append(
                f"| {metric['feature']} | {target} | {float(metric['zero_mae_w'])*1e6:.3f} µW | {fold_mean} | {workload_fold_mean} | {raw} | "
                f"{float(metric['loco_scaled_mae_w'])*1e6:.3f} µW | {better_folds}/12 | {direction} |"
            )
        lines.extend([
            "",
            "## Interpretation",
            "",
            f"The unified set has {changed_pred} placement predictions above 1e-15 W, versus {changed_route} routed signal-net changes. Of the routed changes, {changed_route_zero_place} ({changed_route_zero_place/max(1, changed_route):.2%}) have effectively zero placement prediction.",
            f"For the direct placement power score, per-net LOCO MAE is {float(place_metric['loco_scaled_mae_w'])*1e9:.4f} nW/net versus {float(place_metric['zero_mae_w'])*1e9:.4f} for zero change. Candidate-level raw signal MAE is {float(candidate_signal['raw_mae_w'])*1e6:.3f} µW versus {float(candidate_signal['zero_mae_w'])*1e6:.3f} µW for zero change; full routed total MAE is {float(candidate_total['raw_mae_w'])*1e6:.3f} µW versus {float(candidate_total['zero_mae_w'])*1e6:.3f} µW.",
            f"Placement-score direction matched the routed signal subtotal in {direction_match}/{direction_cases} candidate/workload cases where both values were nonzero.",
            "",
            "Global and workload-conditioned fold means use only the other 11 candidates; the latter predicts each workload from the corresponding workload in the training candidates, making it the stricter baseline. The direct placement score remains too sparse and loses to the workload-conditioned mean. HPWL improves held-out signal-only error relative to zero, but the gain is less decisive against the workload-conditioned mean; it still increases full routed error once clocks are included. Treat HPWL as a signal-only lead, not an accepted placer cost. These are 12 held-out layouts from one design, so more candidate and design diversity is still required. The route-derived wire proxy is an oracle because it depends on SPEF data available only after routing.",
            "",
            "## Files",
            "",
            "- `unified_per_net_features.csv`: combined 309,024-row dataset.",
            "- `candidate_workload_attribution.tsv`: placement estimate and routed signal/clock/total deltas.",
            "- `per_net_loco_metrics.tsv`: per-net metrics for each candidate-held-out feature.",
            "- `candidate_loco_metrics.tsv`: candidate/workload held-out model errors and direction counts.",
            "- `candidate_loco_predictions.tsv`: per-candidate, per-workload held-out predictions and absolute errors for each baseline/feature.",
            "- `candidate_loco_fold_summary.tsv`: three-workload MAE for each held-out candidate, including whether a feature beats that fold's training mean.",
            "- Candidate metrics compare zero, global and workload-conditioned fold means, raw placement score when available, and leave-one-candidate-out feature scaling.",
        ])
        (out / "unified_calibration_report.md").write_text("\n".join(lines) + "\n")
        print(out / "unified_calibration_report.md")
        print(f"Unified rows: {len(rows)}; candidates: {len({str(row['candidate']) for row in rows})}")
        return 0
    except (OSError, KeyError, ValueError) as exc:
        print(f"unified calibration analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
