#!/usr/bin/env python3
"""Fit and externally validate a placement-only proxy for routed CTS clock power."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np

import analyze_cross_design_placement_score as base
import recalibrate_vcd_clock_placement_score as prior


ROOT = Path(__file__).resolve().parents[1]
TRIAL = ROOT / "build/power/gcd_activity_model_compare_20261027"
OUT = ROOT / "build/power/gcd_cts_clock_cost_20261027"
FEATURES = (
    "sum_abs_place_wire_cap_delta_ff",
    "sum_signed_place_wire_cap_delta_ff",
    "sum_abs_hpwl_delta_um",
    "sum_signed_hpwl_delta_um",
    "max_abs_hpwl_delta_um",
)


def read_rows(path: Path, delimiter: str = "\t") -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sign_matches(actual: list[float], predicted: list[float]) -> tuple[int, int]:
    pairs = [(a, p) for a, p in zip(actual, predicted) if abs(a) > 1e-9 and abs(p) > 1e-9]
    return sum((a > 0) == (p > 0) for a, p in pairs), len(pairs)


def mae(actual: list[float], predicted: list[float]) -> float:
    return statistics.mean(abs(a - p) for a, p in zip(actual, predicted))


def rank_corr(actual: list[float], predicted: list[float]) -> float:
    if len(actual) < 2 or len(actual) != len(predicted):
        return float("nan")
    if np.std(actual) < 1e-12 or np.std(predicted) < 1e-12:
        return float("nan")
    return prior.spearman(predicted, actual)


def rho_text(value: float) -> str:
    return f"{value:.3f}" if math.isfinite(value) else "n/a"


def read_current_features() -> dict[str, dict[str, float]]:
    baseline_caps = prior.read_caps(ROOT / "build/power/gcd_baseline20ns/typical.driver_caps.tsv")
    baseline_geom = base.read_geometry(
        ROOT / "build/power/gcd_cross_design_score_20261027/physical/gcd_baseline20ns.nets.csv"
    )
    feature_rows = {}
    for candidate in sorted(row["pair_id"] for row in read_rows(TRIAL / "candidate_pool.tsv")):
        candidate_caps = prior.read_caps(TRIAL / "placement_caps" / candidate / "typical.driver_caps.tsv")
        candidate_geom = base.read_geometry(TRIAL / "physical" / f"{candidate}.nets.csv")
        net_path = TRIAL / "per_net" / candidate / "typical.per_net_delta.csv"
        per_net = {}
        for row in read_rows(net_path, ","):
            net = row["net"]
            if row["baseline_activity_origin"] == "clock" or net.startswith("clknet_"):
                continue
            base_pin = row["baseline_driver_pin"]
            candidate_pin = row["candidate_driver_pin"]
            if not base_pin or base_pin not in baseline_caps or candidate_pin not in candidate_caps:
                raise ValueError(f"placement capacitance join failed for {candidate}/{net}/{base_pin}")
            if net not in baseline_geom or net not in candidate_geom:
                raise ValueError(f"placement geometry join failed for {candidate}/{net}")
            if baseline_caps[base_pin]["net"] != net or candidate_caps[candidate_pin]["net"] != net:
                raise ValueError(f"driver-to-net identity mismatch for {candidate}/{net}")
            baseline = baseline_geom[net]
            placed = candidate_geom[net]
            cap_delta = (float(candidate_caps[candidate_pin]["wire_cap"])
                         - float(baseline_caps[base_pin]["wire_cap"])) * 1e15
            hpwl_delta = float(placed["hpwl_um"]) - float(baseline["hpwl_um"])
            per_net[net] = {
                "delta_place_wire_cap_ff": cap_delta,
                "place_delta_hpwl_um": hpwl_delta,
                "place_abs_delta_hpwl_um": abs(hpwl_delta),
            }
        if len(per_net) != 314:
            raise ValueError(f"expected 314 common signal nets for {candidate}; found {len(per_net)}")
        feature_rows[candidate] = prior.clock_features({
            "per_net": {net: {"features": features} for net, features in per_net.items()}
        })
    return feature_rows


def read_current_labels() -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in read_rows(TRIAL / "route_validation.tsv"):
        grouped[row["candidate"]].append(float(row["routed_clock_delta_nw"]))
    if set(grouped) != {row["pair_id"] for row in read_rows(TRIAL / "candidate_pool.tsv")}:
        raise ValueError("clock route labels do not match the frozen candidate pool")
    labels = {}
    for candidate, values in grouped.items():
        if len(values) != 3 or max(values) - min(values) > 1e-6:
            raise ValueError(f"clock delta must be workload invariant for {candidate}: {values}")
        labels[candidate] = statistics.mean(values)
    return labels


def export_global_route_clock_caps(history: list[dict[str, object]],
                                   current_candidates: list[str]) -> tuple[dict[str, dict[str, object]], list[Path]]:
    openroad = ROOT / "tools/openroad/build/bin/openroad"
    exporter = ROOT / "scripts/gcd_report_global_route_clock_caps.tcl"
    cap_dir = OUT / "grt_clock_caps"
    cap_dir.mkdir(parents=True, exist_ok=True)
    variants = {"baseline": "gcd_baseline20ns"}
    for row in history:
        family, candidate = str(row["candidate"]).split(":", 1)
        suffix = "20261027" if family == "old_gcd" else "exp20261027"
        variants[str(row["candidate"])] = f"gcd_cal_{candidate}_{suffix}"
    variants.update({candidate: f"gcd_modelcmp_{candidate}_20261027" for candidate in current_candidates})

    outputs = {}
    source_paths = []
    baseline_net_names = None
    for label, variant in sorted(variants.items()):
        result_dir = ROOT / "build/orfs/results/sky130hd/gcd_power" / variant
        odb = result_dir / "5_1_grt.odb"
        sdc = result_dir / "5_1_grt.sdc"
        if not odb.is_file() or not sdc.is_file():
            raise FileNotFoundError(f"missing global-route checkpoint/SDC for {label}: {result_dir}")
        before = sha256(odb)
        cap_report = cap_dir / f"{label.replace(':', '_')}.tsv"
        log_path = cap_dir / f"{label.replace(':', '_')}.log"
        env = os.environ.copy()
        env.update({"STAGE_ODB": str(odb), "STAGE_SDC": str(sdc),
                    "CLOCK_CAP_REPORT": str(cap_report)})
        completed = subprocess.run([str(openroad), "-no_init", "-exit", str(exporter)],
                                   cwd=ROOT, env=env, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        log_path.write_text(completed.stdout)
        if completed.returncode:
            raise RuntimeError(f"global-route clock-cap export failed for {label}: {completed.stdout[-3000:]}")
        if sha256(odb) != before:
            raise ValueError(f"read-only GRT clock-cap export modified {odb}")
        nets = {}
        with cap_report.open() as source:
            for line in source:
                if line.startswith("#GRT_CLOCK_CAP\t") and not line.startswith("#GRT_CLOCK_CAP\tnet"):
                    _, net, pin_cap, wire_cap, total_cap, endpoints = line.rstrip("\n").split("\t")
                    nets[net] = {"pin_cap_f": float(pin_cap), "wire_cap_f": float(wire_cap),
                                 "total_cap_f": float(total_cap), "endpoint_count": int(endpoints)}
        if len(nets) != 6:
            raise ValueError(f"expected six CTS clock nets for {label}, found {len(nets)}")
        if baseline_net_names is None:
            baseline_net_names = set(nets)
        elif set(nets) != baseline_net_names:
            raise ValueError(f"clock-net topology changed for {label}: {sorted(set(nets) ^ baseline_net_names)}")
        outputs[label] = {
            "variant": variant,
            "odb": str(odb),
            "odb_sha256": before,
            "net_count": len(nets),
            "nets": nets,
            "pin_cap_ff": sum(row["pin_cap_f"] for row in nets.values()) * 1e15,
            "wire_cap_ff": sum(row["wire_cap_f"] for row in nets.values()) * 1e15,
            "total_cap_ff": sum(row["total_cap_f"] for row in nets.values()) * 1e15,
        }
        source_paths.extend([odb, sdc, cap_report])

    baseline = outputs["baseline"]
    feature_rows = []
    for label, data in sorted(outputs.items()):
        nets = data["nets"]
        data["delta_grt_clock_pin_cap_ff"] = data["pin_cap_ff"] - baseline["pin_cap_ff"]
        data["delta_grt_clock_wire_cap_ff"] = data["wire_cap_ff"] - baseline["wire_cap_ff"]
        data["delta_grt_clock_total_cap_ff"] = data["total_cap_ff"] - baseline["total_cap_ff"]
        row = {
            "candidate": label,
            "variant": data["variant"],
            "odb_sha256": data["odb_sha256"],
            "clock_net_count": data["net_count"],
            "grt_clock_pin_cap_ff": data["pin_cap_ff"],
            "grt_clock_wire_cap_ff": data["wire_cap_ff"],
            "grt_clock_total_cap_ff": data["total_cap_ff"],
            "delta_grt_clock_pin_cap_ff": data["delta_grt_clock_pin_cap_ff"],
            "delta_grt_clock_wire_cap_ff": data["delta_grt_clock_wire_cap_ff"],
            "delta_grt_clock_total_cap_ff": data["delta_grt_clock_total_cap_ff"],
        }
        for net_name in sorted(baseline["nets"]):
            net_short = net_name.replace("clknet_", "").replace("_leaf_clk", "_leaf").replace("_clk", "")
            for cap_kind in ("pin_cap_f", "wire_cap_f", "total_cap_f"):
                row[f"delta_grt_{net_short}_{cap_kind.replace('_cap_f', '_cap_ff')}"] = (
                    nets[net_name][cap_kind] - baseline["nets"][net_name][cap_kind]
                ) * 1e15
        feature_rows.append(row)
    write_rows(OUT / "grt_clock_features.tsv", feature_rows)
    return outputs, source_paths


def fit_through_origin(x: np.ndarray, y: np.ndarray) -> float:
    denominator = float(x @ x)
    return float((x @ y) / denominator) if denominator > 1e-12 else 0.0


def global_route_loco(history: list[dict[str, object]], grt: dict[str, dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, float]]:
    x_all = np.asarray([float(grt[str(row["candidate"])]["delta_grt_clock_wire_cap_ff"])
                        for row in history])
    y_all = np.asarray([float(row["actual_clock_delta_nw"]) for row in history])
    rows = []
    for index, row in enumerate(history):
        mask = np.arange(len(history)) != index
        scale = fit_through_origin(x_all[mask], y_all[mask])
        predicted = scale * x_all[index]
        mean = float(np.mean(y_all[mask]))
        rows.append({
            "candidate": row["candidate"],
            "delta_grt_clock_wire_cap_ff": x_all[index],
            "actual_clock_delta_nw": y_all[index],
            "training_mean_prediction_nw": mean,
            "grt_cap_model_prediction_nw": predicted,
            "grt_calibration_nw_per_ff": scale,
        })
    actual = [float(row["actual_clock_delta_nw"]) for row in rows]
    mean_pred = [float(row["training_mean_prediction_nw"]) for row in rows]
    grt_pred = [float(row["grt_cap_model_prediction_nw"]) for row in rows]
    mean_sign, cases = sign_matches(actual, mean_pred)
    model_sign, model_sign_cases = sign_matches(actual, grt_pred)
    metrics = {
        "candidate_count": len(rows),
        "mean_baseline_mae_nw": mae(actual, mean_pred),
        "grt_cap_model_mae_nw": mae(actual, grt_pred),
        "mean_baseline_sign_matches": float(mean_sign),
        "grt_cap_model_sign_matches": float(model_sign),
        "sign_cases": float(cases),
        "grt_cap_model_sign_cases": float(model_sign_cases),
        "grt_cap_rank_spearman": rank_corr(actual, grt_pred),
    }
    return rows, metrics


def historical_cv(rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], dict[str, float]]:
    predictions = []
    for held in rows:
        train = [row for row in rows if row["candidate"] != held["candidate"]]
        x = np.asarray([[float(row["features"][name]) for name in FEATURES] for row in train])
        y = np.asarray([float(row["actual_clock_delta_nw"]) for row in train])
        test_x = np.asarray([[float(held["features"][name]) for name in FEATURES]])
        mean = float(np.mean(y))
        predicted = float(base.ridge_predict(test_x, base.fit_ridge(x, y, np.ones(len(y))))[0])
        predictions.append({
            "candidate": held["candidate"],
            "actual_clock_delta_nw": held["actual_clock_delta_nw"],
            "training_mean_prediction_nw": mean,
            "ridge_prediction_nw": predicted,
        })
    actual = [float(row["actual_clock_delta_nw"]) for row in predictions]
    mean_pred = [float(row["training_mean_prediction_nw"]) for row in predictions]
    ridge_pred = [float(row["ridge_prediction_nw"]) for row in predictions]
    mean_sign, sign_cases = sign_matches(actual, mean_pred)
    ridge_sign, _ = sign_matches(actual, ridge_pred)
    metrics = {
        "candidate_count": len(rows),
        "mean_baseline_mae_nw": mae(actual, mean_pred),
        "ridge_mae_nw": mae(actual, ridge_pred),
        "mean_baseline_sign_matches": float(mean_sign),
        "ridge_sign_matches": float(ridge_sign),
        "sign_cases": float(sign_cases),
        "ridge_sign_cases": float(sign_matches(actual, ridge_pred)[1]),
        "ridge_rank_spearman": rank_corr(actual, ridge_pred),
    }
    return predictions, metrics


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    _, historical_groups = prior.cached_groups()
    historical_labels = prior.clock_labels()
    history = []
    for group_id, group in sorted(historical_groups.items()):
        label_id = f"{group['family']}:{group['candidate']}"
        if label_id not in historical_labels:
            continue
        history.append({
            "candidate": label_id,
            "features": prior.clock_features(group),
            "actual_clock_delta_nw": historical_labels[label_id],
        })
    if len(history) != 16:
        raise ValueError(f"expected 16 historical training candidates, found {len(history)}")

    current_features = read_current_features()
    current_labels = read_current_labels()
    if set(current_features) != set(current_labels) or len(current_features) != 8:
        raise ValueError("the external validation pool must contain the same eight feature/label candidates")
    held_candidates = sorted(current_features)
    grt_caps, grt_source_paths = export_global_route_clock_caps(history, held_candidates)

    x_train = np.asarray([[float(row["features"][name]) for name in FEATURES] for row in history])
    y_train = np.asarray([float(row["actual_clock_delta_nw"]) for row in history])
    x_test = np.asarray([[float(current_features[c][name]) for name in FEATURES]
                         for c in sorted(current_features)])
    fit = base.fit_ridge(x_train, y_train, np.ones(len(y_train)))
    clock_predictions = base.ridge_predict(x_test, fit)
    training_mean = float(np.mean(y_train))
    predicted_clock = dict(zip(held_candidates, map(float, clock_predictions)))

    history_cv, history_cv_metrics = historical_cv(history)
    actual_clock = [current_labels[c] for c in held_candidates]
    holdout_clock_pred = [predicted_clock[c] for c in held_candidates]
    holdout_mean_pred = [training_mean] * len(held_candidates)
    mean_sign, sign_cases = sign_matches(actual_clock, holdout_mean_pred)
    model_sign, _ = sign_matches(actual_clock, holdout_clock_pred)
    clock_metrics = {
        "external_holdout_candidates": len(held_candidates),
        "training_mean_clock_mae_nw": mae(actual_clock, holdout_mean_pred),
        "ridge_clock_mae_nw": mae(actual_clock, holdout_clock_pred),
        "training_mean_sign_matches": mean_sign,
        "ridge_sign_matches": model_sign,
        "sign_cases": sign_cases,
        "ridge_sign_cases": sign_matches(actual_clock, holdout_clock_pred)[1],
        "ridge_clock_rank_spearman": rank_corr(actual_clock, holdout_clock_pred),
    }

    grt_history = [
        {"candidate": row["candidate"],
         "delta_grt_clock_wire_cap_ff": float(grt_caps[str(row["candidate"])]["delta_grt_clock_wire_cap_ff"]),
         "actual_clock_delta_nw": float(row["actual_clock_delta_nw"])}
        for row in history
    ]
    grt_history_cv, grt_history_cv_metrics = global_route_loco(grt_history, grt_caps)
    grt_train_x = np.asarray([float(row["delta_grt_clock_wire_cap_ff"]) for row in grt_history])
    grt_train_y = np.asarray([float(row["actual_clock_delta_nw"]) for row in grt_history])
    grt_scale = fit_through_origin(grt_train_x, grt_train_y)
    grt_nonzero_training = int(np.count_nonzero(np.abs(grt_train_x) > 1e-9))
    grt_nonzero_holdout = int(sum(abs(float(grt_caps[c]["delta_grt_clock_wire_cap_ff"])) > 1e-9
                                  for c in held_candidates))
    grt_clock_predictions = {
        candidate: grt_scale * float(grt_caps[candidate]["delta_grt_clock_wire_cap_ff"])
        for candidate in held_candidates
    }
    grt_clock_pred = [grt_clock_predictions[c] for c in held_candidates]
    grt_sign, grt_sign_cases = sign_matches(actual_clock, grt_clock_pred)
    grt_clock_metrics = {
        "external_holdout_candidates": len(held_candidates),
        "training_mean_clock_mae_nw": mae(actual_clock, holdout_mean_pred),
        "grt_clock_mae_nw": mae(actual_clock, grt_clock_pred),
        "training_mean_sign_matches": mean_sign,
        "grt_sign_matches": grt_sign,
        "sign_cases": sign_cases,
        "grt_sign_cases": grt_sign_cases,
        "grt_clock_rank_spearman": rank_corr(actual_clock, grt_clock_pred),
        "calibrated_nw_per_ff": grt_scale,
    }

    score_rows = {row["pair_id"]: row for row in read_rows(TRIAL / "candidate_scores_vcd_gamma1.tsv")}
    routed_rows = {row["candidate"]: row for row in read_rows(TRIAL / "candidate_route_summary.tsv")}
    if set(score_rows) != set(held_candidates) or set(routed_rows) != set(held_candidates):
        raise ValueError("signal scores or routed outcomes do not match the clock holdout pool")
    total_predictions = []
    for candidate in held_candidates:
        signal_pred = float(score_rows[candidate]["predicted_worst_workload_delta_nw"])
        actual_total = float(routed_rows[candidate]["routed_worst_total_power_delta_nw"])
        actual_signal = float(routed_rows[candidate]["routed_worst_signal_delta_nw"])
        total_predictions.append({
            "candidate": candidate,
            "predicted_signal_delta_nw": signal_pred,
            "predicted_clock_delta_nw": predicted_clock[candidate],
            "grt_cap_clock_delta_nw": grt_clock_predictions[candidate],
            "delta_grt_clock_wire_cap_ff": float(grt_caps[candidate]["delta_grt_clock_wire_cap_ff"]),
            "training_mean_clock_delta_nw": training_mean,
            "actual_worst_signal_delta_nw": actual_signal,
            "actual_worst_total_delta_nw": actual_total,
            "signal_only_total_estimate_nw": signal_pred,
            "mean_clock_total_estimate_nw": signal_pred + training_mean,
            "ridge_clock_total_estimate_nw": signal_pred + predicted_clock[candidate],
            "grt_clock_total_estimate_nw": signal_pred + grt_clock_predictions[candidate],
            "actual_clock_delta_nw": current_labels[candidate],
            "all_workload_total_power_reduction": routed_rows[candidate]["all_workload_total_power_reduction"],
        })
    total_metrics = []
    actual_totals = [float(row["actual_worst_total_delta_nw"]) for row in total_predictions]
    for model_name, field in (
        ("signal_only", "signal_only_total_estimate_nw"),
        ("signal_plus_training_mean_clock", "mean_clock_total_estimate_nw"),
        ("signal_plus_external_ridge_clock", "ridge_clock_total_estimate_nw"),
        ("signal_plus_calibrated_grt_clock", "grt_clock_total_estimate_nw"),
    ):
        predicted = [float(row[field]) for row in total_predictions]
        order = sorted(range(len(predicted)), key=predicted.__getitem__)
        actual_best_index = actual_totals.index(min(actual_totals))
        best_actual_in_top3 = int(actual_best_index in order[:3])
        total_metrics.append({
            "model": model_name,
            "worst_total_power_mae_nw": mae(actual_totals, predicted),
            "worst_total_rank_spearman": rank_corr(actual_totals, predicted),
            "actual_best_candidate_in_predicted_top3": best_actual_in_top3,
            "predicted_best_candidate": held_candidates[order[0]],
            "actual_best_candidate": held_candidates[actual_totals.index(min(actual_totals))],
        })

    write_rows(OUT / "historical_clock_loco.tsv", history_cv)
    write_rows(OUT / "historical_grt_clock_loco.tsv", grt_history_cv)
    write_rows(OUT / "clock_holdout_predictions.tsv", [
        {"candidate": candidate, **current_features[candidate],
         "training_mean_clock_delta_nw": training_mean,
         "predicted_clock_delta_nw": predicted_clock[candidate],
         "delta_grt_clock_wire_cap_ff": float(grt_caps[candidate]["delta_grt_clock_wire_cap_ff"]),
         "grt_cap_clock_prediction_nw": grt_clock_predictions[candidate],
         "grt_cap_absolute_error_nw": abs(grt_clock_predictions[candidate] - current_labels[candidate]),
         "actual_clock_delta_nw": current_labels[candidate],
         "training_mean_absolute_error_nw": abs(training_mean - current_labels[candidate]),
         "model_absolute_error_nw": abs(predicted_clock[candidate] - current_labels[candidate])}
        for candidate in held_candidates
    ])
    write_rows(OUT / "combined_total_predictions.tsv", total_predictions)
    write_rows(OUT / "clock_cost_metrics.tsv", [
        {"scope": "historical_leave_one_candidate_out", **history_cv_metrics},
        {"scope": "new_pool_external_holdout", **clock_metrics},
        {"scope": "historical_grt_leave_one_candidate_out", **grt_history_cv_metrics},
        {"scope": "new_pool_grt_external_holdout", **grt_clock_metrics},
        *[{"scope": "new_pool_total_power", **row} for row in total_metrics],
    ])

    model = {
        "model": "ridge_placement_proxy_for_routed_cts_clock_delta_power",
        "target": "routed_clock_delta_nw",
        "features": list(FEATURES),
        "alpha": base.ALPHA,
        "training_candidates": [row["candidate"] for row in history],
        "training_candidate_count": len(history),
        "external_holdout_candidates": held_candidates,
        "qualification_status": "not_qualified_external_clock_mae_worse_than_training_mean",
        "weighted_feature_mean": fit[0].tolist(),
        "weighted_feature_scale": fit[1].tolist(),
        "standardized_coefficients": fit[2].tolist(),
        "intercept_nw": fit[3],
        "training_mean_clock_delta_nw": training_mean,
        "note": "Experimental placement-only proxy. It has not been qualified for placer use; route-level CTS/SPEF remains authoritative.",
    }
    model_path = OUT / "clock_cost_model.json"
    model_path.write_text(json.dumps(model, indent=2) + "\n")

    grt_model = {
        "model": "calibrated_global_route_clock_wire_cap_to_routed_clock_power",
        "target": "routed_clock_delta_nw",
        "feature": "delta_grt_clock_wire_cap_ff",
        "calibration": "through_origin_scalar_fit_on_16_historical_route_safe_GCD_candidates",
        "calibrated_nw_per_ff": grt_scale,
        "training_candidates": [row["candidate"] for row in history],
        "training_candidate_count": len(history),
        "external_holdout_candidates": held_candidates,
        "nonzero_training_clock_cap_deltas": grt_nonzero_training,
        "nonzero_external_clock_cap_deltas": grt_nonzero_holdout,
        "qualification_status": "not_qualified_no_external_clock_cap_variation_or_rank_signal",
        "note": "Uses clock parasitics estimated after global routing to predict detailed-route SPEF clock power; candidate screening model, not a pre-CTS placer objective.",
    }
    grt_model_path = OUT / "grt_clock_cost_model.json"
    grt_model_path.write_text(json.dumps(grt_model, indent=2) + "\n")

    inputs = {
        ROOT / "build/power/gcd_cross_design_score_20261027/export_manifest.tsv",
        base.OLD_ROUTE_VALIDATION, base.EXP_ROUTE_VALIDATION,
        base.OLD_CAP_MANIFEST, base.EXP_CAP_MANIFEST,
        TRIAL / "candidate_pool.tsv", TRIAL / "frozen_pre_route_manifest.tsv",
        TRIAL / "candidate_scores_vcd_gamma1.tsv", TRIAL / "candidate_route_summary.tsv",
        TRIAL / "route_validation.tsv",
        ROOT / "build/power/gcd_baseline20ns/typical.driver_caps.tsv",
        ROOT / "build/power/gcd_cross_design_score_20261027/physical/gcd_baseline20ns.nets.csv",
    }
    for candidate in held_candidates:
        inputs.add(TRIAL / "physical" / f"{candidate}.nets.csv")
        inputs.add(TRIAL / "placement_caps" / candidate / "typical.driver_caps.tsv")
        inputs.add(TRIAL / "per_net" / candidate / "typical.per_net_delta.csv")
    inputs.update(grt_source_paths)
    for manifest_path in (base.OLD_CAP_MANIFEST, base.EXP_CAP_MANIFEST,
                          ROOT / "build/power/gcd_cross_design_score_20261027/export_manifest.tsv"):
        for row in read_rows(manifest_path):
            for key in ("cap_report", "physical_csv", "odb"):
                if row.get(key):
                    value = Path(row[key])
                    if value.is_file():
                        inputs.add(value)
    hash_rows = [{"path": str(path), "sha256": sha256(path)} for path in sorted(inputs) if path.is_file()]
    write_rows(OUT / "input_hashes.tsv", hash_rows)

    signal = next(row for row in total_metrics if row["model"] == "signal_only")
    combined = next(row for row in total_metrics if row["model"] == "signal_plus_calibrated_grt_clock")
    qualified = (grt_clock_metrics["grt_clock_mae_nw"] < grt_clock_metrics["training_mean_clock_mae_nw"]
                 and combined["worst_total_rank_spearman"] > signal["worst_total_rank_spearman"]
                 and combined["worst_total_power_mae_nw"] < signal["worst_total_power_mae_nw"])
    lines = [
        "# CTS clock-cost proxy: external candidate holdout",
        "",
        f"A ridge model was fit on {len(history)} earlier route-safe GCD candidates and evaluated on eight frozen candidates that were excluded from fitting. Features use placement-stage signal-net geometry and capacitance deltas; labels use routed clock-net switching-power deltas from SPEF.",
        "",
        "## Clock prediction",
        "",
        "| Evaluation | Candidates | Mean baseline MAE (nW) | Ridge MAE (nW) | Mean sign matches | Ridge sign matches | Ridge rank ρ |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Historical leave-one-candidate-out | {history_cv_metrics['candidate_count']} | {history_cv_metrics['mean_baseline_mae_nw']:.2f} | {history_cv_metrics['ridge_mae_nw']:.2f} | {int(history_cv_metrics['mean_baseline_sign_matches'])}/{int(history_cv_metrics['sign_cases'])} | {int(history_cv_metrics['ridge_sign_matches'])}/{int(history_cv_metrics['ridge_sign_cases'])} | {rho_text(history_cv_metrics['ridge_rank_spearman'])} |",
        f"| New-pool external holdout | {clock_metrics['external_holdout_candidates']} | {clock_metrics['training_mean_clock_mae_nw']:.2f} | {clock_metrics['ridge_clock_mae_nw']:.2f} | {clock_metrics['training_mean_sign_matches']}/{clock_metrics['sign_cases']} | {clock_metrics['ridge_sign_matches']}/{clock_metrics['ridge_sign_cases']} | {rho_text(clock_metrics['ridge_clock_rank_spearman'])} |",
        "",
        "The placement-only ridge did not improve external clock MAE over the training-mean baseline, despite better sign accuracy. Clock-tree buffer count, clock-net count, and CTS average sink wire length were invariant across the eight new candidates.",
        "",
        "## Global-route clock-capacitance calibration",
        "",
        f"A second estimate uses clock wire capacitance estimated from each `5_1_grt.odb`, calibrated on the same {len(history)} historical candidates and evaluated on the eight untouched candidates. This stage is available after global routing and before detailed routing.",
        "",
        "| Evaluation | Candidates | Mean baseline MAE (nW) | GRT-cap model MAE (nW) | Mean sign matches | GRT sign matches | GRT rank ρ |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Historical leave-one-candidate-out | {grt_history_cv_metrics['candidate_count']} | {grt_history_cv_metrics['mean_baseline_mae_nw']:.2f} | {grt_history_cv_metrics['grt_cap_model_mae_nw']:.2f} | {int(grt_history_cv_metrics['mean_baseline_sign_matches'])}/{int(grt_history_cv_metrics['sign_cases'])} | {int(grt_history_cv_metrics['grt_cap_model_sign_matches'])}/{int(grt_history_cv_metrics['grt_cap_model_sign_cases'])} | {rho_text(grt_history_cv_metrics['grt_cap_rank_spearman'])} |",
        f"| New-pool external holdout | {grt_clock_metrics['external_holdout_candidates']} | {grt_clock_metrics['training_mean_clock_mae_nw']:.2f} | {grt_clock_metrics['grt_clock_mae_nw']:.2f} | {grt_clock_metrics['training_mean_sign_matches']}/{grt_clock_metrics['sign_cases']} | {grt_clock_metrics['grt_sign_matches']}/{grt_clock_metrics['grt_sign_cases']} | {rho_text(grt_clock_metrics['grt_clock_rank_spearman'])} |",
        "",
        f"The GRT clock wire-cap delta was nonzero for {grt_nonzero_training}/{len(history)} historical training candidates and {grt_nonzero_holdout}/{len(held_candidates)} external candidates. All eight external clock-cap estimates were therefore zero; their lower MAE than the shifted training mean reflects this holdout pool's signed-delta distribution, not candidate discrimination (rank correlation is undefined and sign coverage is 0/0).",
        "",
        "## Combined total-power ranking",
        "",
        "The VCD-weighted signal predictions are fixed from the pre-route score file. The clock term is added at candidate level because the measured clock delta is identical across idle, typical, and peak for each placement.",
        "",
        "| Estimate | Worst-total MAE (nW) | Worst-total rank ρ | Actual best candidate in predicted top 3 | Predicted best | Actual best |",
        "|---|---:|---:|---:|---|---|",
    ]
    for row in total_metrics:
        lines.append(f"| {row['model']} | {float(row['worst_total_power_mae_nw']):.2f} | {rho_text(float(row['worst_total_rank_spearman']))} | {row['actual_best_candidate_in_predicted_top3']} | {row['predicted_best_candidate']} | {row['actual_best_candidate']} |")
    lines.extend([
        "",
        f"GRT clock-cost qualification gate (beat the mean prior on external clock MAE, then improve combined worst-total MAE and rank over signal-only): **{'PASS' if qualified else 'FAIL'}**.",
        "",
        "The CTS log metrics are invariant in this candidate set. The placement-only statistical proxy failed external MAE. The calibrated GRT estimate is available only after clock-tree synthesis and global routing, so it is a candidate-screening cost rather than a pre-CTS placer objective. Keep route-level SPEF as the acceptance measurement.",
        "",
        "Files: `clock_cost_metrics.tsv`, `clock_holdout_predictions.tsv`, `combined_total_predictions.tsv`, `grt_clock_features.tsv`, `historical_clock_loco.tsv`, `historical_grt_clock_loco.tsv`, `clock_cost_model.json`, `grt_clock_cost_model.json`, and `input_hashes.tsv`.",
    ])
    (OUT / "clock_cost_report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
