#!/usr/bin/env python3
"""Cross-validate placement-only predictors of routed SPEF capacitance change."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE**2
WORKLOADS = ("idle", "typical", "peak")
STAGES = ("cts_placement", "global_route")
UNIFIED = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01/unified_per_net_features.csv"
STAGE_DATA = ROOT / "build/power/place_spef_cts_grt_20261027_locsearch01/stage_signal_per_net_deltas.tsv"
STAGE_REPORT = ROOT / "build/power/place_spef_cts_grt_20261027_locsearch01/candidate_workload_stage_attribution.tsv"
PAIR_DIR = ROOT / "build/power/vcd_swap_audit_20261027_locsearch01/pair_calibration"
PAIR_ODB_DIR = ROOT / "build/power/vcd_swap_audit_20261027_locsearch01/candidate_odb"
BASELINE_PHYSICAL = ROOT / "build/power/place_spef_expansion_physical_20261027_calset01a/baseline_placement.nets.csv"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
PHYSICAL_TCL = ROOT / "scripts/export_net_physical_features.tcl"
PAIR_CANDIDATES = ("swap44", "swap52")
PLACEMENT_TERMS = (
    "delta_place_wire_cap_ff",
    "delta_place_load_cap_ff",
    "place_delta_hpwl_um",
    "place_abs_delta_hpwl_um",
    "place_delta_bbox_width_um",
    "place_delta_bbox_height_um",
    "place_baseline_fanout",
    "place_delta_fanout",
    "place_delta_endpoint_count",
    "place_baseline_hpwl_um",
    "place_baseline_endpoint_count",
)
STAGE_TERMS = ("cts_estimated_delta_cap_ff", "global_route_estimated_delta_cap_ff")
FEATURE_TERMS = PLACEMENT_TERMS + STAGE_TERMS
GEOMETRY_TERMS = tuple(term for term in PLACEMENT_TERMS if not term.startswith("delta_place_"))
MODEL_TERMS = {
    "zero_cap_delta": (),
    "placement_wire_direct": ("delta_place_wire_cap_ff",),
    "placement_load_direct": ("delta_place_load_cap_ff",),
    "cts_cap_direct": ("cts_estimated_delta_cap_ff",),
    "global_route_cap_direct": ("global_route_estimated_delta_cap_ff",),
    "ridge_geometry": GEOMETRY_TERMS,
    "ridge_cap_geometry": PLACEMENT_TERMS,
    "ridge_cap_geometry_activity_weighted": PLACEMENT_TERMS,
    "ridge_geometry_grt": GEOMETRY_TERMS + STAGE_TERMS,
    "ridge_all_available_grt_activity_weighted": FEATURE_TERMS,
}
RIDGE_ALPHA = 1.0
EPS = 1.0e-24


def read_csv(path: Path, delimiter: str = ","):
    with path.open(newline="") as source:
        yield from csv.DictReader(source, delimiter=delimiter)


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


def number(row: dict[str, str], name: str, optional: bool = False) -> float:
    value = row.get(name, "")
    if optional and value == "":
        raise ValueError(f"missing placement feature {name}")
    return float(value or 0.0)


def read_net_geometry(path: Path, common_nets: set[str] | None = None) -> dict[str, dict[str, float]]:
    data: dict[str, dict[str, float]] = {}
    for row in read_csv(path):
        net = row["net"]
        if common_nets is not None and net not in common_nets:
            continue
        if row["sig_type"] != "SIGNAL":
            continue
        dbu = float(row["dbu_per_micron"])
        data[net] = {
            "fanout": float(row["fanout"]),
            "endpoint_count": float(row["endpoint_count"]),
            "hpwl_um": float(row["hpwl_dbu"]) / dbu,
            "bbox_width_um": float(row["bbox_width_dbu"]) / dbu,
            "bbox_height_um": float(row["bbox_height_dbu"]) / dbu,
        }
    return data


def export_pair_geometry(output: Path, pair_hashes: dict[str, str]) -> tuple[dict[str, dict[str, dict[str, float]]], list[dict[str, object]]]:
    output.mkdir(parents=True, exist_ok=True)
    baseline = read_net_geometry(BASELINE_PHYSICAL)
    baseline_hash = sha256(BASELINE_PHYSICAL)
    result: dict[str, dict[str, dict[str, float]]] = {}
    manifest = [{"candidate": "baseline", "source": str(BASELINE_PHYSICAL), "sha256": baseline_hash}]
    for candidate in PAIR_CANDIDATES:
        odb = PAIR_ODB_DIR / f"{candidate}.odb"
        expected_hash = pair_hashes.get(str(odb.resolve()))
        if not expected_hash or sha256(odb) != expected_hash:
            raise ValueError(f"{candidate} ODB does not match the pair-calibration input hash")
        net_path = output / f"{candidate}.placement.nets.csv"
        layer_path = output / f"{candidate}.placement.layers.csv"
        env = os.environ.copy()
        env.update({
            "FEATURE_ODB": str(odb),
            "FEATURE_STAGE": "placement",
            "FEATURE_NET_CSV": str(net_path),
            "FEATURE_LAYER_CSV": str(layer_path),
        })
        completed = subprocess.run(
            [str(OPENROAD), "-no_init", "-exit", str(PHYSICAL_TCL)],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        (output / f"{candidate}.placement.log").write_text(completed.stdout)
        if completed.returncode != 0:
            raise ValueError(f"OpenROAD physical feature export failed for {candidate}; see {output}")
        if sha256(odb) != expected_hash:
            raise ValueError(f"OpenROAD changed the source candidate ODB for {candidate}")
        data = read_net_geometry(net_path, set(baseline))
        if len(data) < 8584:
            raise ValueError(f"only {len(data)} signal nets exported for {candidate}")
        result[candidate] = data
        manifest.append({"candidate": candidate, "source": str(odb), "sha256": expected_hash,
                         "net_csv": str(net_path), "signal_nets": len(data)})
    return result, manifest


def fit_ridge(x: np.ndarray, y: np.ndarray, weights: np.ndarray, alpha: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    total = float(weights.sum())
    if total <= 0:
        weights = np.ones_like(y)
        total = float(weights.sum())
    mean = np.average(x, axis=0, weights=weights)
    variance = np.average((x - mean) ** 2, axis=0, weights=weights)
    std = np.sqrt(variance)
    std[std < 1.0e-12] = 1.0
    z = (x - mean) / std
    y_mean = float(np.average(y, weights=weights))
    weighted_z = z * np.sqrt(weights)[:, None]
    weighted_y = (y - y_mean) * np.sqrt(weights)
    lhs = weighted_z.T @ weighted_z + alpha * np.eye(x.shape[1])
    rhs = weighted_z.T @ weighted_y
    coef = np.linalg.solve(lhs, rhs)
    return mean, std, coef, y_mean


def predict_ridge(x: np.ndarray, model: tuple[np.ndarray, np.ndarray, np.ndarray, float]) -> np.ndarray:
    mean, std, coef, intercept = model
    return intercept + ((x - mean) / std) @ coef


def mean_absolute_error(a: list[float], b: list[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/power/place_spef_placement_cap_predictor_20261027")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    try:
        for path in (UNIFIED, STAGE_DATA, STAGE_REPORT, BASELINE_PHYSICAL, OPENROAD, PHYSICAL_TCL,
                     PAIR_DIR / "input_hashes_before.tsv", PAIR_DIR / "input_hashes_after.tsv"):
            if not path.exists():
                raise FileNotFoundError(path)
        if (PAIR_DIR / "input_hashes_before.tsv").read_bytes() != (PAIR_DIR / "input_hashes_after.tsv").read_bytes():
            raise ValueError("pair calibration input hashes differ before/after the routed experiment")
        pair_hashes = {}
        for line in (PAIR_DIR / "input_hashes_before.tsv").read_text().splitlines():
            if "\t" in line:
                digest, path = line.split("\t", 1)
                pair_hashes[str(Path(path).resolve())] = digest

        # Read candidate/workload activity and routed labels, while keeping one feature row per net.
        candidates: set[str] = set()
        common_nets: set[str] = set()
        feature_values: dict[tuple[str, str], dict[str, float]] = {}
        cap_labels: dict[tuple[str, str], float] = {}
        activity: dict[tuple[str, str, str], float] = {}
        candidate_density: dict[tuple[str, str, str], float] = {}
        routed_signal: dict[tuple[str, str], float] = defaultdict(float)
        repeat_count: dict[tuple[str, str], int] = defaultdict(int)
        for row in read_csv(UNIFIED):
            if row["routed_class"] != "common_signal":
                continue
            candidate, workload, net = row["candidate"], row["workload"], row["net"]
            if workload not in WORKLOADS:
                raise ValueError(f"unexpected workload {workload}")
            candidates.add(candidate)
            common_nets.add(net)
            key = (candidate, net)
            feature = {
                "delta_place_wire_cap_ff": number(row, "delta_place_wire_cap_f") * 1e15,
                "delta_place_load_cap_ff": number(row, "delta_place_load_cap_f") * 1e15,
                "place_delta_hpwl_um": number(row, "place_delta_hpwl_um"),
                "place_abs_delta_hpwl_um": number(row, "place_abs_delta_hpwl_um"),
                "place_delta_bbox_width_um": number(row, "place_delta_bbox_width_um"),
                "place_delta_bbox_height_um": number(row, "place_delta_bbox_height_um"),
                "place_baseline_fanout": number(row, "place_baseline_fanout"),
                "place_delta_fanout": number(row, "place_delta_fanout"),
                "place_delta_endpoint_count": number(row, "place_delta_endpoint_count"),
                "place_baseline_hpwl_um": number(row, "place_baseline_hpwl_um"),
                "place_baseline_endpoint_count": number(row, "place_baseline_endpoint_count"),
            }
            prior = feature_values.get(key)
            if prior is not None and any(not math.isclose(prior[name], value, rel_tol=1e-10, abs_tol=1e-12)
                                         for name, value in feature.items()):
                raise ValueError(f"placement feature varies across workloads for {candidate}/{net}")
            feature_values[key] = feature
            target = number(row, "routed_delta_wire_cap_f") * 1e15
            old_target = cap_labels.get(key)
            if old_target is not None and not math.isclose(old_target, target, rel_tol=1e-10, abs_tol=1e-10):
                raise ValueError(f"SPEF cap label varies across workloads for {candidate}/{net}")
            cap_labels[key] = target
            activity[(candidate, workload, net)] = number(row, "baseline_place_density_hz")
            candidate_density[(candidate, workload, net)] = number(row, "candidate_place_density_hz")
            routed_signal[(candidate, workload)] += number(row, "routed_power_delta_w")
            repeat_count[key] += 1

        if len(candidates) != 12 or len(common_nets) != 8584:
            raise ValueError(f"expected 12 candidates × 8,584 common signal nets, got {len(candidates)} × {len(common_nets)}")
        if any(value != len(WORKLOADS) for value in repeat_count.values()):
            raise ValueError("unified data does not contain exactly three workload rows per candidate/net")

        # Read pre-detailed-route cap estimates and CTS baseline activity context.
        stage_delta_features: dict[str, dict[tuple[str, str], float]] = {stage: {} for stage in STAGES}
        stage_context: dict[tuple[str, str, str], tuple[float, float, float]] = {}
        stage_repeats: dict[tuple[str, str, str], int] = defaultdict(int)
        for row in read_csv(STAGE_DATA, delimiter="\t"):
            stage = row["stage"]
            key = (row["candidate"], row["net"])
            delta = (float(row["candidate_cap_f"]) - float(row["baseline_cap_f"])) * 1e15
            prior = stage_delta_features[stage].get(key)
            if prior is not None and not math.isclose(prior, delta, rel_tol=1e-10, abs_tol=1e-9):
                raise ValueError(f"{stage} capacitance delta varies across workloads for {key}")
            stage_delta_features[stage][key] = delta
            stage_repeats[(stage, row["candidate"], row["net"])] += 1
            if stage == "cts_placement":
                context_key = (row["candidate"], row["workload"], row["net"])
                stage_context[context_key] = (float(row["baseline_cap_f"]),
                                              float(row["baseline_density_hz"]),
                                              float(row["candidate_density_hz"]))
        for stage in STAGES:
            if len(stage_delta_features[stage]) != len(feature_values) or any(
                count != len(WORKLOADS)
                for (seen_stage, _, _), count in stage_repeats.items() if seen_stage == stage
            ):
                raise ValueError(f"{stage} capacitance features are incomplete")
        for key in feature_values:
            feature_values[key]["cts_estimated_delta_cap_ff"] = stage_delta_features["cts_placement"][key]
            feature_values[key]["global_route_estimated_delta_cap_ff"] = stage_delta_features["global_route"][key]

        # Complete placement geometry for the two pair-swap layouts whose original
        # joined feature table did not contain ODB-derived topology measurements.
        geometry_dir = out / "placement_geometry"
        pair_geometry, geometry_manifest = export_pair_geometry(geometry_dir, pair_hashes)
        baseline_geometry = read_net_geometry(BASELINE_PHYSICAL, common_nets)
        if len(baseline_geometry) != len(common_nets):
            raise ValueError("baseline placement geometry does not cover all common signal nets")
        for candidate in PAIR_CANDIDATES:
            candidate_geometry = pair_geometry[candidate]
            for net in common_nets:
                if net not in baseline_geometry or net not in candidate_geometry:
                    raise ValueError(f"placement geometry missing {candidate}/{net}")
                base, cand = baseline_geometry[net], candidate_geometry[net]
                feature = feature_values[(candidate, net)]
                feature.update({
                    "place_delta_hpwl_um": cand["hpwl_um"] - base["hpwl_um"],
                    "place_abs_delta_hpwl_um": abs(cand["hpwl_um"] - base["hpwl_um"]),
                    "place_delta_bbox_width_um": cand["bbox_width_um"] - base["bbox_width_um"],
                    "place_delta_bbox_height_um": cand["bbox_height_um"] - base["bbox_height_um"],
                    "place_baseline_fanout": base["fanout"],
                    "place_delta_fanout": cand["fanout"] - base["fanout"],
                    "place_delta_endpoint_count": cand["endpoint_count"] - base["endpoint_count"],
                    "place_baseline_hpwl_um": base["hpwl_um"],
                    "place_baseline_endpoint_count": base["endpoint_count"],
                })

        keys = sorted(feature_values)
        if len(keys) != len(candidates) * len(common_nets):
            raise ValueError("placement feature coverage is incomplete")
        cand_list = sorted(candidates)
        x_all = np.asarray([[feature_values[key][name] for name in FEATURE_TERMS] for key in keys], dtype=float)
        y_all = np.asarray([cap_labels[key] for key in keys], dtype=float)
        candidate_for_row = np.asarray([key[0] for key in keys], dtype=object)
        index_for_key = {key: idx for idx, key in enumerate(keys)}
        # The fitting weight is the mean baseline VCD transition rate across workloads.
        weights = np.asarray([
            sum(activity[(key[0], workload, key[1])] for workload in WORKLOADS) / len(WORKLOADS)
            for key in keys
        ], dtype=float)
        if weights.max(initial=0.0) > 0:
            weights = weights / weights[weights > 0].mean()

        model_predictions: dict[str, dict[tuple[str, str], float]] = {
            name: {} for name in MODEL_TERMS
        }
        fold_models: list[dict[str, object]] = []
        coefficient_rows: list[dict[str, object]] = []
        for held in cand_list:
            train_mask = candidate_for_row != held
            test_mask = candidate_for_row == held
            for model_name, terms in MODEL_TERMS.items():
                test_indices = np.nonzero(test_mask)[0]
                if model_name == "zero_cap_delta":
                    test_pred = np.zeros(len(test_indices), dtype=float)
                    model = None
                elif model_name == "placement_wire_direct":
                    test_pred = x_all[test_indices, FEATURE_TERMS.index("delta_place_wire_cap_ff")]
                    model = None
                elif model_name == "placement_load_direct":
                    test_pred = x_all[test_indices, FEATURE_TERMS.index("delta_place_load_cap_ff")]
                    model = None
                elif model_name == "cts_cap_direct":
                    test_pred = x_all[test_indices, FEATURE_TERMS.index("cts_estimated_delta_cap_ff")]
                    model = None
                elif model_name == "global_route_cap_direct":
                    test_pred = x_all[test_indices, FEATURE_TERMS.index("global_route_estimated_delta_cap_ff")]
                    model = None
                else:
                    col_indices = [FEATURE_TERMS.index(term) for term in terms]
                    train_x = x_all[train_mask][:, col_indices]
                    test_x = x_all[test_mask][:, col_indices]
                    train_y = y_all[train_mask]
                    train_w = weights[train_mask] if model_name.endswith("activity_weighted") else np.ones(len(train_y))
                    model = fit_ridge(train_x, train_y, train_w, RIDGE_ALPHA)
                    test_pred = predict_ridge(test_x, model)
                for key, value in zip((keys[idx] for idx in test_indices), test_pred):
                    model_predictions[model_name][key] = float(value)
                if model is not None:
                    mean, std, coef, intercept = model
                    fold_models.append({"held_out_candidate": held, "model": model_name,
                                        "intercept_ff": intercept, "ridge_alpha": RIDGE_ALPHA})
                    for term, m, s, c in zip(terms, mean, std, coef):
                        coefficient_rows.append({"held_out_candidate": held, "model": model_name,
                                                 "feature": term, "mean": m, "scale": s,
                                                 "standardized_coefficient_ff": c,
                                                 "coefficient_per_unit": c / s})

        # Cap-only power target and full routed signal-power target.
        cap_target: dict[tuple[str, str], float] = defaultdict(float)
        full_target = routed_signal
        for candidate in cand_list:
            for workload in WORKLOADS:
                for net in common_nets:
                    cap_target[(candidate, workload)] += (
                        POWER_FACTOR * activity[(candidate, workload, net)]
                        * cap_labels[(candidate, net)] * 1e-15
                    )

        # CTS baseline cap is used to reconstruct total switching power; learned
        # ΔC features can be placement-only or include pre-detailed-route estimates.
        if len(stage_context) != len(candidates) * len(WORKLOADS) * len(common_nets):
            raise ValueError("CTS context does not cover every candidate/workload/signal-net row")

        full_predictions: dict[str, dict[tuple[str, str], float]] = {name: defaultdict(float) for name in MODEL_TERMS}
        cap_predictions: dict[str, dict[tuple[str, str], float]] = {name: defaultdict(float) for name in MODEL_TERMS}
        per_net_rows: list[dict[str, object]] = []
        for key in keys:
            candidate, net = key
            label = cap_labels[key]
            row = {"candidate": candidate, "net": net, "routed_spef_wire_cap_delta_ff": label}
            row.update(feature_values[key])
            for model_name in MODEL_TERMS:
                pred = model_predictions[model_name][key]
                row[f"{model_name}_predicted_delta_ff"] = pred
                row[f"{model_name}_absolute_error_ff"] = abs(pred - label)
            per_net_rows.append(row)
            for workload in WORKLOADS:
                base_rate = activity[(candidate, workload, net)]
                cand_rate = candidate_density[(candidate, workload, net)]
                base_cap, ctx_base_rate, ctx_candidate_rate = stage_context[(candidate, workload, net)]
                # Stage cap-table densities should match the unified VCD annotation.
                if not math.isclose(base_rate, ctx_base_rate, rel_tol=1e-5, abs_tol=100.0) or not math.isclose(
                    cand_rate, ctx_candidate_rate, rel_tol=1e-5, abs_tol=100.0
                ):
                    raise ValueError(f"VCD activity mismatch for {candidate}/{workload}/{net}")
                for model_name in MODEL_TERMS:
                    pred = model_predictions[model_name][key] * 1e-15
                    pred = max(pred, -base_cap)  # preserve nonnegative predicted candidate capacitance
                    cap_predictions[model_name][(candidate, workload)] += POWER_FACTOR * base_rate * pred
                    full_predictions[model_name][(candidate, workload)] += POWER_FACTOR * (
                        (base_cap + pred) * cand_rate - base_cap * base_rate
                    )

        metric_rows: list[dict[str, object]] = []
        fold_rows: list[dict[str, object]] = []
        for target_name, actual_map, predictions in (
            ("routed_wire_cap_power_proxy", cap_target, cap_predictions),
            ("full_routed_signal_power", full_target, full_predictions),
        ):
            baseline: dict[tuple[str, str], float] = {}
            for held in cand_list:
                for workload in WORKLOADS:
                    train_vals = [actual_map[(candidate, workload)] for candidate in cand_list if candidate != held]
                    baseline[(held, workload)] = sum(train_vals) / len(train_vals)
            model_set = ("workload_mean", *MODEL_TERMS.keys())
            for model_name in model_set:
                predicted_map = baseline if model_name == "workload_mean" else predictions[model_name]
                actual_values = [actual_map[(candidate, workload)] for candidate in cand_list for workload in WORKLOADS]
                predicted_values = [predicted_map[(candidate, workload)] for candidate in cand_list for workload in WORKLOADS]
                wins = 0
                for held in cand_list:
                    actual_fold = [actual_map[(held, workload)] for workload in WORKLOADS]
                    pred_fold = [predicted_map[(held, workload)] for workload in WORKLOADS]
                    mean_fold = [baseline[(held, workload)] for workload in WORKLOADS]
                    fold_mae = mean_absolute_error(actual_fold, pred_fold)
                    mean_mae = mean_absolute_error(actual_fold, mean_fold)
                    beats = fold_mae < mean_mae - 1e-18
                    wins += int(beats)
                    fold_rows.append({"target": target_name, "model": model_name,
                                      "held_out_candidate": held, "mae_uw": fold_mae * 1e6,
                                      "workload_mean_mae_uw": mean_mae * 1e6,
                                      "beats_workload_mean": beats})
                metric_rows.append({"target": target_name, "model": model_name,
                                    "candidate_workload_rows": len(actual_values),
                                    "mae_uw": mean_absolute_error(actual_values, predicted_values) * 1e6,
                                    "folds_beating_workload_mean": "baseline" if model_name == "workload_mean" else f"{wins}/12"})

        cap_metric_rows: list[dict[str, object]] = []
        for model_name, predicted in model_predictions.items():
            errors = [abs(predicted[key] - cap_labels[key]) for key in keys]
            weighted_error = 0.0
            weight_sum = 0.0
            for key in keys:
                for workload in WORKLOADS:
                    rate = activity[(key[0], workload, key[1])]
                    weighted_error += POWER_FACTOR * rate * abs(predicted[key] - cap_labels[key]) * 1e-15
                    weight_sum += 1
            cap_metric_rows.append({
                "model": model_name,
                "candidate_net_rows": len(keys),
                "per_net_cap_mae_ff": sum(errors) / len(errors),
                "activity_weighted_cap_power_abs_error_nw_per_net_workload": weighted_error / weight_sum * 1e9,
                "nonzero_prediction_pct": 100.0 * sum(abs(predicted[key]) > 1e-12 for key in keys) / len(keys),
            })

        # Independent source hashes and timing guard summary.
        timing_rows = list(read_csv(STAGE_REPORT, delimiter="\t"))
        timing_counts = {
            stage: sum(row["timing_guard_50ps_pass"] == "True" for row in timing_rows if row["stage"] == stage)
            for stage in STAGES
        }
        if timing_counts != {"cts_placement": 36, "global_route": 36}:
            raise ValueError(f"existing stage timing guard no longer passes all cases: {timing_counts}")
        out.mkdir(parents=True, exist_ok=True)
        write_table(out / "candidate_workload_predictor_metrics.tsv", metric_rows)
        write_table(out / "candidate_workload_predictor_folds.tsv", fold_rows)
        write_table(out / "per_net_predictor_metrics.tsv", cap_metric_rows)
        write_table(out / "per_net_heldout_predictions.csv", per_net_rows, delimiter=",")
        write_table(out / "ridge_fold_models.tsv", fold_models)
        write_table(out / "ridge_coefficients.tsv", coefficient_rows)
        write_table(out / "pair_placement_geometry_manifest.tsv", geometry_manifest)
        source_paths = (UNIFIED, STAGE_DATA, STAGE_REPORT, BASELINE_PHYSICAL,
                        PAIR_DIR / "input_hashes_before.tsv", PAIR_DIR / "input_hashes_after.tsv")
        write_table(out / "source_hashes.tsv", [{"path": str(path), "sha256": sha256(path)} for path in source_paths])

        metric_map = {(r["target"], r["model"]): r for r in metric_rows}
        lines = [
            "# Placement and pre-route capacitance predictor evaluation",
            "",
            f"Evaluated {len(cand_list)} candidate layouts × {len(common_nets)} common signal nets. Entire candidate layouts were held out across all workloads. Models use placement HPWL/bbox/fanout/capacitance features; hybrid variants also use CTS and global-route estimated ΔC. Detailed-route centerline, via, layer, and SPEF target fields were excluded from model inputs. Physical placement features for swap44 and swap52 were exported from their candidate ODBs, with source hashes checked against the prior calibration manifest.",
            "",
            f"The existing 50 ps setup/hold guard remains {timing_counts['cts_placement']}/36 CTS and {timing_counts['global_route']}/36 global-route cases. Fitted models use a standardized linear ridge fit (alpha={RIDGE_ALPHA:g}); activity-weighted variants use mean baseline VCD transition rate across the three workloads as their training weight.",
            "",
            "## Candidate/workload held-out results",
            "",
            "MAE is averaged over 36 candidate/workload samples. The workload mean uses only the other 11 candidates. Full routed signal power uses the existing CTS baseline capacitance to reconstruct the activity term; the learned ΔC comes from the input features listed by each model.",
            "",
            "| Target | Model | MAE | Held-out folds beating workload mean |",
            "|---|---|---:|---:|",
        ]
        for target in ("routed_wire_cap_power_proxy", "full_routed_signal_power"):
            for model_name in ("workload_mean", *MODEL_TERMS.keys()):
                row = metric_map[(target, model_name)]
                lines.append(f"| {target} | {model_name} | {float(row['mae_uw']):.4f} µW | {row['folds_beating_workload_mean']} |")
        lines.extend([
            "",
            "## Per-net capacitance prediction",
            "",
            "| Model | ΔC MAE | Activity-weighted absolute power error | Nonzero predictions |",
            "|---|---:|---:|---:|",
        ])
        for row in cap_metric_rows:
            lines.append(f"| {row['model']} | {float(row['per_net_cap_mae_ff']):.4f} fF | "
                         f"{float(row['activity_weighted_cap_power_abs_error_nw_per_net_workload']):.4f} nW/net/workload | "
                         f"{float(row['nonzero_prediction_pct']):.2f}% |")
        full_base = float(metric_map[("full_routed_signal_power", "workload_mean")]["mae_uw"])
        best_model = min((name for name in MODEL_TERMS),
                         key=lambda name: float(metric_map[("full_routed_signal_power", name)]["mae_uw"]))
        best_full = float(metric_map[("full_routed_signal_power", best_model)]["mae_uw"])
        best_full_wins = metric_map[("full_routed_signal_power", best_model)]["folds_beating_workload_mean"]
        base_cap = float(metric_map[("routed_wire_cap_power_proxy", "workload_mean")]["mae_uw"])
        best_cap_model = min(MODEL_TERMS, key=lambda name: float(metric_map[("routed_wire_cap_power_proxy", name)]["mae_uw"]))
        best_cap = float(metric_map[("routed_wire_cap_power_proxy", best_cap_model)]["mae_uw"])
        lines.extend([
            "",
            "## Decision",
            "",
            f"Best model for full routed signal power was `{best_model}` at {best_full:.4f} µW versus the workload-conditioned baseline at {full_base:.4f} µW. Best cap-only model was `{best_cap_model}` at {best_cap:.4f} µW versus {base_cap:.4f} µW. The best full-signal model wins only {best_full_wins} held-out layouts, so it does not qualify for placer integration.",
            "",
            "## Outputs",
            "",
            "- `candidate_workload_predictor_metrics.tsv` and `candidate_workload_predictor_folds.tsv`: held-out candidate-level power results.",
            "- `per_net_predictor_metrics.tsv` and `per_net_heldout_predictions.csv`: SPEF capacitance labels and held-out per-net predictions.",
            "- `ridge_coefficients.tsv`: fold-specific model coefficients.",
            "- `pair_placement_geometry_manifest.tsv`: ODB hashes and placement feature exports for the two pair swaps.",
            "- `source_hashes.tsv`: hashes of analysis sources.",
        ])
        report = out / "placement_cap_predictor_report.md"
        report.write_text("\n".join(lines) + "\n")
        print(f"Evaluated placement/pre-route ΔC predictors for {len(cand_list)} held-out candidates × {len(common_nets)} nets")
        print(report)
        return 0
    except (OSError, KeyError, ValueError, np.linalg.LinAlgError, subprocess.SubprocessError) as exc:
        print(f"placement capacitance predictor analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
