#!/usr/bin/env python3
"""Evaluate placement-only routed-cap/power predictors with candidate-held-out folds.

The strict transfer set trains on the existing PicoRV32 candidates plus the
older safe GCD swaps, then predicts the expanded safe GCD swaps. A second set
of folds holds out each expanded GCD layout and compares placement-only models
with and without placement-timing criticality features.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE**2
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
PHYSICAL_TCL = ROOT / "scripts/export_net_physical_features.tcl"
TIMING_TCL = ROOT / "scripts/export_net_timing_criticality.tcl"
GCD_BASE = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
GCD_BASELINE_ODB = GCD_BASE / "3_5_place_dp.odb"
GCD_SDC = GCD_BASE / "3_place.sdc"
PICO_FEATURES = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01/unified_per_net_features.csv"
OLD_ROUTE_VALIDATION = ROOT / "build/power/gcd_swap_cal_20261027/route_analysis/route_validation.tsv"
OLD_ODB_DIR = ROOT / "build/power/gcd_swap_cal_20261027/candidate_odb_relaxed"
OLD_CAP_DIR = ROOT / "build/power/cross_design_wirecap_20261027/placement_caps"
OLD_CAP_MANIFEST = OLD_CAP_DIR / "export_manifest.tsv"
EXP_ROUTE_VALIDATION = ROOT / "build/power/gcd_swap_cal_expansion_20261027/route_analysis/route_validation.tsv"
EXP_ODB_DIR = ROOT / "build/power/gcd_swap_cal_expansion_20261027/candidate_odb"
EXP_CAP_DIR = ROOT / "build/power/gcd_swap_cal_expansion_20261027/placement_caps"
EXP_CAP_MANIFEST = EXP_CAP_DIR / "export_manifest.tsv"

GEOMETRY_TERMS = (
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
CAP_TERMS = ("delta_place_wire_cap_ff", "delta_place_load_cap_ff")
TIMING_TERMS = (
    "setup_criticality_baseline",
    "setup_criticality_candidate",
    "setup_criticality_delta",
    "setup_min_slack_baseline_ns",
    "setup_min_slack_delta_ns",
    "setup_constrained_baseline",
    "setup_constrained_candidate",
)
MODEL_TERMS = {
    "ridge_geometry": GEOMETRY_TERMS,
    "ridge_cap_geometry": CAP_TERMS + GEOMETRY_TERMS,
    "ridge_cap_geometry_timing": CAP_TERMS + GEOMETRY_TERMS + TIMING_TERMS,
}
ALPHA = 1.0


def read_table(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def write_table(path: Path, rows: list[dict[str, object]], delimiter: str = "\t") -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def number(row: dict[str, str] | dict[str, object], key: str) -> float:
    return float(row.get(key, 0) or 0.0)


def close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)


def fit_ridge(x: np.ndarray, y: np.ndarray, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    if x.ndim != 2 or not len(y):
        raise ValueError("ridge fit needs a nonempty two-dimensional feature matrix")
    if weights.sum() <= 0:
        weights = np.ones_like(y)
    mean = np.average(x, axis=0, weights=weights)
    variance = np.average((x - mean) ** 2, axis=0, weights=weights)
    scale = np.sqrt(variance)
    scale[scale < 1e-12] = 1.0
    z = (x - mean) / scale
    intercept = float(np.average(y, weights=weights))
    wz = z * np.sqrt(weights)[:, None]
    wy = (y - intercept) * np.sqrt(weights)
    coef = np.linalg.solve(wz.T @ wz + ALPHA * np.eye(x.shape[1]), wz.T @ wy)
    return mean, scale, coef, intercept


def ridge_predict(x: np.ndarray, model: tuple[np.ndarray, np.ndarray, np.ndarray, float]) -> np.ndarray:
    mean, scale, coef, intercept = model
    return intercept + ((x - mean) / scale) @ coef


def candidate_weights(group_ids: list[str]) -> np.ndarray:
    counts: dict[str, int] = defaultdict(int)
    for group in group_ids:
        counts[group] += 1
    return np.asarray([1.0 / counts[group] for group in group_ids], dtype=float)


def fit_scale(x: np.ndarray, y: np.ndarray, weights: np.ndarray) -> float:
    denom = float(np.sum(weights * x * x))
    return float(np.sum(weights * x * y) / denom) if denom else 0.0


def mae(actual: list[float], predicted: list[float]) -> float:
    if len(actual) != len(predicted) or not actual:
        raise ValueError("MAE inputs must be nonempty and equal in length")
    return statistics.mean(abs(a - p) for a, p in zip(actual, predicted))


def sign_counts(actual: list[float], predicted: list[float]) -> tuple[int, int]:
    pairs = [(a, p) for a, p in zip(actual, predicted) if abs(a) > 1e-18 and abs(p) > 1e-18]
    return sum((a > 0) == (p > 0) for a, p in pairs), len(pairs)


def route_safe_candidates(path: Path) -> list[str]:
    by_candidate: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in read_table(path, "\t"):
        by_candidate[row["pair_id"]].append(row)
    safe = []
    for candidate, rows in sorted(by_candidate.items()):
        if len(rows) != len(WORKLOADS):
            continue
        if all(number(row, "setup_loss_ns") >= -0.05 and number(row, "hold_loss_ns") >= -0.05 for row in rows):
            safe.append(candidate)
    return safe


def read_cap_report(path: Path) -> dict[str, dict[str, float | str]]:
    result = {}
    with path.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 6 or fields[1] == "pin":
                continue
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            result[pin] = {"net": net, "pin_cap": float(pin_cap), "wire_cap": float(wire_cap),
                           "total_cap": float(total_cap)}
    if not result:
        raise ValueError(f"no driver-cap rows in {path}")
    return result


def read_geometry(path: Path) -> dict[str, dict[str, float]]:
    result = {}
    for row in read_table(path):
        if row["sig_type"] != "SIGNAL":
            continue
        dbu = number(row, "dbu_per_micron")
        result[row["net"]] = {
            "fanout": number(row, "fanout"),
            "endpoint_count": number(row, "endpoint_count"),
            "hpwl_um": number(row, "hpwl_dbu") / dbu,
            "bbox_width_um": number(row, "bbox_width_dbu") / dbu,
            "bbox_height_um": number(row, "bbox_height_dbu") / dbu,
        }
    return result


def read_timing(path: Path) -> dict[str, dict[str, float]]:
    result = {}
    for row in read_table(path, "\t"):
        result[row["net"]] = {
            "criticality": number(row, "setup_criticality"),
            "min_slack_ns": number(row, "setup_min_sink_slack_ns"),
            "constrained": 1.0,
        }
    return result


def export_odb_features(odb: Path, label: str, out: Path) -> tuple[Path, Path, dict[str, object]]:
    if not odb.is_file():
        raise FileNotFoundError(odb)
    before = sha256(odb)
    physical_csv = out / "physical" / f"{label}.nets.csv"
    layer_csv = out / "physical" / f"{label}.layers.csv"
    timing_tsv = out / "timing" / f"{label}.tsv"
    for path in (physical_csv, layer_csv, timing_tsv):
        path.parent.mkdir(parents=True, exist_ok=True)

    common = os.environ.copy()
    env = common.copy()
    env.update({"FEATURE_ODB": str(odb), "FEATURE_STAGE": "placement",
                "FEATURE_NET_CSV": str(physical_csv), "FEATURE_LAYER_CSV": str(layer_csv)})
    physical = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(PHYSICAL_TCL)], cwd=ROOT,
                              env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (out / "physical" / f"{label}.log").write_text(physical.stdout)
    if physical.returncode:
        raise RuntimeError(f"physical export failed for {label}: {physical.stdout[-2000:]}")
    env = common.copy()
    env.update({"TIMING_ODB": str(odb), "TIMING_SDC": str(GCD_SDC), "TIMING_FEATURE_OUT": str(timing_tsv)})
    timing = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(TIMING_TCL)], cwd=ROOT,
                            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    (out / "timing" / f"{label}.log").write_text(timing.stdout)
    if timing.returncode:
        raise RuntimeError(f"timing export failed for {label}: {timing.stdout[-2000:]}")
    after = sha256(odb)
    if before != after:
        raise ValueError(f"read-only feature export modified {odb}")
    return physical_csv, timing_tsv, {"label": label, "odb": str(odb), "odb_sha256": before,
                                      "physical_csv": str(physical_csv), "physical_sha256": sha256(physical_csv),
                                      "timing_tsv": str(timing_tsv), "timing_sha256": sha256(timing_tsv)}


def validate_cap_manifest(path: Path, report_dir: Path) -> dict[str, Path]:
    rows = read_table(path, "\t")
    result: dict[str, Path] = {}
    latest: dict[str, dict[str, str]] = {}
    for row in rows:
        latest.setdefault(row["candidate"], row)
        if row["workload"] == "idle":
            latest[row["candidate"]] = row
        report = Path(row["cap_report"])
        digest_key = "cap_sha256" if "cap_sha256" in row else "cap_report_sha256"
        if sha256(report) != row[digest_key]:
            raise ValueError(f"placement cap report hash mismatch: {report}")
        odb_hash_key = "odb_sha256" if "odb_sha256" in row else "odb_sha256_before"
        after_key = "odb_sha256_after" if "odb_sha256_after" in row else odb_hash_key
        if row.get(odb_hash_key) != row.get(after_key):
            raise ValueError(f"placement ODB before/after hashes differ in cap manifest: {row.get('odb', '')}")
        if sha256(Path(row["odb"])) != row[odb_hash_key]:
            raise ValueError(f"placement ODB hash mismatch in cap manifest: {row['odb']}")
    for candidate, row in latest.items():
        if candidate == "baseline":
            continue
        path_value = Path(row["odb"])
        expected_dir = report_dir
        result[candidate] = path_value
    return result


def build_gcd_groups(out: Path, old_candidates: list[str], expanded_candidates: list[str]) -> tuple[dict[str, dict[str, object]], list[dict[str, object]]]:
    # Validate the already-produced placement-cap exports and their source ODB hashes.
    old_odb_map = validate_cap_manifest(OLD_CAP_MANIFEST, OLD_CAP_DIR)
    exp_odb_map = validate_cap_manifest(EXP_CAP_MANIFEST, EXP_CAP_DIR)
    old_sources: dict[str, Path] = {}
    exp_sources: dict[str, Path] = {}
    for candidate in old_candidates:
        src = OLD_ODB_DIR / f"{candidate}.odb"
        if old_odb_map.get(candidate) and old_odb_map[candidate].resolve() != src.resolve():
            raise ValueError(f"old cap manifest ODB mismatch for {candidate}")
        old_sources[candidate] = src
    for candidate in expanded_candidates:
        src = EXP_ODB_DIR / f"{candidate}.odb"
        if exp_odb_map.get(candidate) and exp_odb_map[candidate].resolve() != src.resolve():
            raise ValueError(f"expanded cap manifest ODB mismatch for {candidate}")
        exp_sources[candidate] = src

    layouts: list[tuple[str, Path]] = [("gcd_baseline20ns", GCD_BASELINE_ODB)]
    layouts.extend((f"old_{candidate}", old_sources[candidate]) for candidate in old_candidates)
    layouts.extend((f"expanded_{candidate}", exp_sources[candidate]) for candidate in expanded_candidates)
    exports: dict[str, tuple[Path, Path]] = {}
    manifest = []
    for label, odb in layouts:
        physical, timing, row = export_odb_features(odb, label, out)
        exports[label] = (physical, timing)
        manifest.append(row)

    base_geom = read_geometry(exports["gcd_baseline20ns"][0])
    base_timing = read_timing(exports["gcd_baseline20ns"][1])
    groups: dict[str, dict[str, object]] = {}
    for family, candidates, cap_dir, route_root, tag in (
        ("old_gcd", old_candidates, OLD_CAP_DIR, ROOT / "build/power", "20261027"),
        ("expanded_gcd", expanded_candidates, EXP_CAP_DIR, ROOT / "build/power", "exp20261027"),
    ):
        for candidate in candidates:
            group_id = f"{family}:{candidate}"
            label = f"old_{candidate}" if family == "old_gcd" else f"expanded_{candidate}"
            cand_geom = read_geometry(exports[label][0])
            cand_timing = read_timing(exports[label][1])
            baseline_caps = {workload: read_cap_report(cap_dir / "baseline" / f"{workload}.driver_caps.tsv")
                             for workload in WORKLOADS}
            candidate_caps = {workload: read_cap_report(cap_dir / candidate / f"{workload}.driver_caps.tsv")
                              for workload in WORKLOADS}
            per_net: dict[str, dict[str, object]] = {}
            for workload in WORKLOADS:
                route_path = route_root / f"gcd_cal_{candidate}_{tag}" / "net_attribution" / f"{workload}.per_net_delta.csv"
                if not route_path.is_file():
                    raise FileNotFoundError(route_path)
                for item in read_table(route_path):
                    if item["baseline_activity_origin"] == "clock" or item["net"].startswith("clknet_"):
                        continue
                    net = item["net"]
                    pin = item["baseline_driver_pin"]
                    if not pin or pin not in baseline_caps[workload] or pin not in candidate_caps[workload]:
                        raise ValueError(f"placement-cap driver join failed for {group_id}/{workload}/{net}/{pin}")
                    if net not in base_geom or net not in cand_geom:
                        raise ValueError(f"placement topology join failed for {group_id}/{net}")
                    base_cap = baseline_caps[workload][pin]
                    cand_cap = candidate_caps[workload][pin]
                    if str(base_cap["net"]) != net or str(cand_cap["net"]) != net:
                        raise ValueError(f"driver-pin net mismatch for {group_id}/{workload}/{net}/{pin}")
                    base, cand = base_geom[net], cand_geom[net]
                    btime = base_timing.get(net, {"criticality": 0.0, "min_slack_ns": 0.0, "constrained": 0.0})
                    ctime = cand_timing.get(net, {"criticality": 0.0, "min_slack_ns": 0.0, "constrained": 0.0})
                    features = {
                        "delta_place_wire_cap_ff": (float(cand_cap["wire_cap"]) - float(base_cap["wire_cap"])) * 1e15,
                        "delta_place_load_cap_ff": (float(cand_cap["total_cap"]) - float(base_cap["total_cap"])) * 1e15,
                        "place_delta_hpwl_um": cand["hpwl_um"] - base["hpwl_um"],
                        "place_abs_delta_hpwl_um": abs(cand["hpwl_um"] - base["hpwl_um"]),
                        "place_delta_bbox_width_um": cand["bbox_width_um"] - base["bbox_width_um"],
                        "place_delta_bbox_height_um": cand["bbox_height_um"] - base["bbox_height_um"],
                        "place_baseline_fanout": base["fanout"],
                        "place_delta_fanout": cand["fanout"] - base["fanout"],
                        "place_delta_endpoint_count": cand["endpoint_count"] - base["endpoint_count"],
                        "place_baseline_hpwl_um": base["hpwl_um"],
                        "place_baseline_endpoint_count": base["endpoint_count"],
                        "setup_criticality_baseline": btime["criticality"],
                        "setup_criticality_candidate": ctime["criticality"],
                        "setup_criticality_delta": ctime["criticality"] - btime["criticality"],
                        "setup_min_slack_baseline_ns": btime["min_slack_ns"],
                        "setup_min_slack_delta_ns": ctime["min_slack_ns"] - btime["min_slack_ns"],
                        "setup_constrained_baseline": btime["constrained"],
                        "setup_constrained_candidate": ctime["constrained"],
                    }
                    target_cap_ff = number(item, "delta_wire_cap_f") * 1e15
                    target_power = number(item, "delta_switching_power_w")
                    record = per_net.setdefault(net, {"candidate": group_id, "source": family, "net": net,
                                                       "features": features, "target_cap_ff": target_cap_ff,
                                                       "workloads": {}})
                    if not close(float(record["target_cap_ff"]), target_cap_ff):
                        raise ValueError(f"routed ΔC differs by workload for {group_id}/{net}")
                    record["workloads"][workload] = {"density_hz": number(item, "baseline_density_hz"),
                                                     "routed_power_w": target_power}
            if len(per_net) != 314:
                raise ValueError(f"expected 314 common GCD signal nets for {group_id}, got {len(per_net)}")
            if any(set(row["workloads"]) != set(WORKLOADS) for row in per_net.values()):
                raise ValueError(f"incomplete workload labels for {group_id}")
            groups[group_id] = {"family": family, "candidate": candidate, "per_net": per_net}
    return groups, manifest


def build_pico_groups() -> dict[str, dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    for row in read_table(PICO_FEATURES):
        if row["routed_class"] != "common_signal":
            continue
        group_id = f"pico:{row['candidate']}"
        net = row["net"]
        features = {
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
        group = groups.setdefault(group_id, {"family": "pico", "candidate": row["candidate"], "per_net": {}})
        target_cap_ff = number(row, "routed_delta_wire_cap_f") * 1e15
        record = group["per_net"].setdefault(net, {"candidate": group_id, "source": "pico", "net": net,
                                                   "features": features, "target_cap_ff": target_cap_ff,
                                                   "workloads": {}})
        if not close(float(record["target_cap_ff"]), target_cap_ff):
            raise ValueError(f"Pico routed ΔC differs by workload for {group_id}/{net}")
        workload = row["workload"]
        record["workloads"][workload] = {"density_hz": number(row, "baseline_place_density_hz"),
                                         "routed_power_w": number(row, "routed_power_delta_w")}
    if len(groups) != 12:
        raise ValueError(f"expected 12 Pico candidates, got {len(groups)}")
    for group_id, group in groups.items():
        if len(group["per_net"]) != 8584:
            raise ValueError(f"expected 8,584 Pico common signal nets for {group_id}")
        if any(set(row["workloads"]) != set(WORKLOADS) for row in group["per_net"].values()):
            raise ValueError(f"incomplete Pico workloads for {group_id}")
    return groups


def flatten_groups(groups: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    return [record for group in groups.values() for record in group["per_net"].values()]


def model_predictions(train: list[dict[str, object]], test: list[dict[str, object]], include_timing: bool) -> dict[str, np.ndarray]:
    all_models: dict[str, np.ndarray] = {
        "zero": np.zeros(len(test), dtype=float),
        "placement_wire_direct": np.asarray([r["features"]["delta_place_wire_cap_ff"] for r in test], dtype=float),
    }
    train_groups = [str(r["candidate"]) for r in train]
    weights = candidate_weights(train_groups)
    train_y = np.asarray([float(r["target_cap_ff"]) for r in train], dtype=float)
    train_xcap = np.asarray([float(r["features"]["delta_place_wire_cap_ff"]) for r in train], dtype=float)
    direct_scale = fit_scale(train_xcap, train_y, weights)
    test_xcap = all_models["placement_wire_direct"]
    all_models["train_only_cap_scale"] = direct_scale * test_xcap
    model_names = ["ridge_geometry", "ridge_cap_geometry"]
    if include_timing:
        model_names.append("ridge_cap_geometry_timing")
    for name in model_names:
        terms = MODEL_TERMS[name]
        train_x = np.asarray([[float(r["features"].get(term, 0.0)) for term in terms] for r in train], dtype=float)
        test_x = np.asarray([[float(r["features"].get(term, 0.0)) for term in terms] for r in test], dtype=float)
        all_models[name] = ridge_predict(test_x, fit_ridge(train_x, train_y, weights))
    return all_models


def evaluate_scope(scope: str, training_groups: dict[str, dict[str, object]], test_groups: dict[str, dict[str, object]],
                   include_timing: bool, candidate_fold: bool = False) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    metrics: list[dict[str, object]] = []
    predictions_table: list[dict[str, object]] = []
    per_net_table: list[dict[str, object]] = []
    held_candidates = sorted(test_groups)
    folds = held_candidates if candidate_fold else ["__all__"]
    for held in folds:
        train_rows = flatten_groups(training_groups)
        if candidate_fold:
            train_rows += flatten_groups({k: v for k, v in test_groups.items() if k != held})
            test_subset = {held: test_groups[held]}
        else:
            test_subset = test_groups
        test_rows = flatten_groups(test_subset)
        preds = model_predictions(train_rows, test_rows, include_timing)
        test_actual = np.asarray([float(r["target_cap_ff"]) for r in test_rows], dtype=float)
        model_names = list(preds)
        for model_name, predicted in preds.items():
            correct, cases = sign_counts(test_actual.tolist(), predicted.tolist())
            metrics.append({"scope": scope, "fold": held, "target": "routed ΔC", "model": model_name,
                            "rows": len(test_rows), "mae": mae(test_actual.tolist(), predicted.tolist()),
                            "unit": "fF", "sign_matches": correct, "sign_cases": cases})
        for row, actual, values in zip(test_rows, test_actual, zip(*(preds[name] for name in model_names))):
            net = str(row["net"])
            candidate = str(row["candidate"])
            for model_name, pred in zip(model_names, values):
                per_net_table.append({"scope": scope, "fold": held, "candidate": candidate, "net": net,
                                      "model": model_name, "routed_delta_wire_cap_ff": float(actual),
                                      "predicted_delta_wire_cap_ff": float(pred)})
        # Convert predicted per-net ΔC into per-workload switching-power deltas.
        test_index = {id(row): i for i, row in enumerate(test_rows)}
        power_actual: dict[tuple[str, str], float] = defaultdict(float)
        power_pred: dict[tuple[str, str, str], float] = defaultdict(float)
        for row in test_rows:
            i = test_index[id(row)]
            candidate = str(row["candidate"])
            for workload, observed in row["workloads"].items():
                key = (candidate, workload)
                power_actual[key] += float(observed["routed_power_w"])
                for model_name, predicted in preds.items():
                    power_pred[(candidate, workload, model_name)] += (
                        POWER_FACTOR * float(observed["density_hz"]) * float(predicted[i]) * 1e-15
                    )
        for (candidate, workload), actual in sorted(power_actual.items()):
            pred_by_model = {name: power_pred[(candidate, workload, name)] for name in model_names}
            for model_name, predicted in pred_by_model.items():
                predictions_table.append({"scope": scope, "fold": held, "candidate": candidate,
                                          "workload": workload, "model": model_name,
                                          "routed_signal_power_delta_nw": actual * 1e9,
                                          "predicted_signal_power_delta_nw": predicted * 1e9})
        actual_power = [row["routed_signal_power_delta_nw"] for row in predictions_table
                        if row["scope"] == scope and row["fold"] == held]
        for model_name in model_names:
            selected = [row for row in predictions_table if row["scope"] == scope and row["fold"] == held
                        and row["model"] == model_name]
            predicted_power = [float(row["predicted_signal_power_delta_nw"]) for row in selected]
            actual_for_model = [float(row["routed_signal_power_delta_nw"]) for row in selected]
            correct, cases = sign_counts(actual_for_model, predicted_power)
            metrics.append({"scope": scope, "fold": held, "target": "routed signal power", "model": model_name,
                            "rows": len(selected), "mae": mae(actual_for_model, predicted_power),
                            "unit": "nW", "sign_matches": correct, "sign_cases": cases})
    return metrics, predictions_table, per_net_table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "build/power/gcd_cross_design_score_20261027")
    parser.add_argument("--skip-export", action="store_true", help="reuse matching physical/timing feature exports")
    args = parser.parse_args()
    out = args.output_dir.resolve()
    try:
        inputs = (OPENROAD, PHYSICAL_TCL, TIMING_TCL, GCD_BASELINE_ODB, GCD_SDC, PICO_FEATURES,
                  OLD_ROUTE_VALIDATION, OLD_CAP_MANIFEST, EXP_ROUTE_VALIDATION, EXP_CAP_MANIFEST)
        for path in inputs:
            if not path.is_file():
                raise FileNotFoundError(path)
        old_candidates = route_safe_candidates(OLD_ROUTE_VALIDATION)
        expanded_candidates = route_safe_candidates(EXP_ROUTE_VALIDATION)
        if len(old_candidates) != 7 or len(expanded_candidates) != 9:
            raise ValueError(f"expected 7 old and 9 expanded routed-safe GCD candidates, got {len(old_candidates)} and {len(expanded_candidates)}")

        if args.skip_export:
            manifest_path = out / "export_manifest.tsv"
            if not manifest_path.is_file():
                raise FileNotFoundError(f"--skip-export needs {manifest_path}")
            export_manifest = read_table(manifest_path, "\t")
            for row in export_manifest:
                if sha256(Path(row["odb"])) != row["odb_sha256"]:
                    raise ValueError(f"ODB changed after export: {row['odb']}")
                for key, hash_key in (("physical_csv", "physical_sha256"), ("timing_tsv", "timing_sha256")):
                    if sha256(Path(row[key])) != row[hash_key]:
                        raise ValueError(f"saved {key} hash mismatch: {row[key]}")
        else:
            export_manifest = None
        # build_gcd_groups performs all exports unless cached files were requested.
        if args.skip_export:
            # Reconstruct and verify the cache using the exact expected candidate labels.
            expected = {"gcd_baseline20ns", *[f"old_{c}" for c in old_candidates],
                        *[f"expanded_{c}" for c in expanded_candidates]}
            if {row["label"] for row in export_manifest} != expected:
                raise ValueError("cached feature-export candidate set does not match the routed-safe inputs")
            # Export function checks hashes; these exports are intentionally re-used by pointing it at
            # the already exported inputs below through a temporary identity function.
            def cached_export(odb: Path, label: str, _out: Path) -> tuple[Path, Path, dict[str, object]]:
                row = next(r for r in export_manifest if r["label"] == label)
                if Path(row["odb"]).resolve() != odb.resolve():
                    raise ValueError(f"cached ODB path mismatch for {label}")
                return Path(row["physical_csv"]), Path(row["timing_tsv"]), row
            original_export = export_odb_features
            globals()["export_odb_features"] = cached_export
            try:
                gcd_groups, generated_manifest = build_gcd_groups(out, old_candidates, expanded_candidates)
            finally:
                globals()["export_odb_features"] = original_export
            export_manifest = generated_manifest
        else:
            gcd_groups, export_manifest = build_gcd_groups(out, old_candidates, expanded_candidates)
        write_table(out / "export_manifest.tsv", export_manifest)

        pico_groups = build_pico_groups()
        old_groups = {k: v for k, v in gcd_groups.items() if v["family"] == "old_gcd"}
        expanded_groups = {k: v for k, v in gcd_groups.items() if v["family"] == "expanded_gcd"}
        if len(old_groups) != 7 or len(expanded_groups) != 9:
            raise ValueError("GCD group construction lost candidates")

        # Fit the deployment model on every previously routed, timing-safe
        # calibration layout. Give each placement candidate equal total
        # weight so Pico's larger netlist does not dominate the GCD examples.
        deployment_groups = {**pico_groups, **gcd_groups}
        deployment_rows = flatten_groups(deployment_groups)
        deployment_terms = MODEL_TERMS["ridge_cap_geometry"]
        deployment_x = np.asarray(
            [[float(row["features"].get(term, 0.0)) for term in deployment_terms]
             for row in deployment_rows], dtype=float)
        deployment_y = np.asarray(
            [float(row["target_cap_ff"]) for row in deployment_rows], dtype=float)
        deployment_weights = candidate_weights([str(row["candidate"]) for row in deployment_rows])
        deployment_mean, deployment_scale, deployment_coef, deployment_intercept = fit_ridge(
            deployment_x, deployment_y, deployment_weights)
        model_path = out / "trained_ridge_cap_geometry.json"
        model = {
            "model": "ridge_cap_geometry",
            "target": "routed_signal_delta_wire_cap_ff",
            "alpha": ALPHA,
            "terms": list(deployment_terms),
            "weighted_feature_mean": deployment_mean.tolist(),
            "weighted_feature_scale": deployment_scale.tolist(),
            "standardized_coefficients": deployment_coef.tolist(),
            "intercept_ff": deployment_intercept,
            "candidate_weighting": "equal total weight per routed-safe placement candidate",
            "training_candidate_ids": sorted(deployment_groups),
            "training_candidate_count": len(deployment_groups),
            "training_net_rows": len(deployment_rows),
            "training_source_counts": {
                "pico": len(pico_groups), "older_gcd": len(old_groups),
                "expanded_gcd": len(expanded_groups),
            },
            "note": "Prediction label is routed signal-net delta wire capacitance; this placement model does not predict CTS clock-tree changes.",
        }
        model_path.write_text(json.dumps(model, indent=2) + "\n")
        print(f"Deployment model trained on {len(deployment_groups)} routed-safe candidates; saved {model_path}")

        # Strict design transfer: old candidate groups are the only GCD labels used in training.
        transfer_train = {**pico_groups, **old_groups}
        transfer_metrics, transfer_power_rows, transfer_net_rows = evaluate_scope(
            "Pico + older GCD → expanded GCD", transfer_train, expanded_groups, include_timing=False)
        # Candidate-held-out GCD LOCO: all workloads of one entire layout stay in the test fold.
        loco_metrics, loco_power_rows, loco_net_rows = evaluate_scope(
            "expanded GCD LOCO", {}, expanded_groups, include_timing=True, candidate_fold=True)
        metrics = transfer_metrics + loco_metrics
        route_totals = {}
        for row in read_table(EXP_ROUTE_VALIDATION, "\t"):
            route_totals[(row["pair_id"], row["workload"])] = row
        power_rows = transfer_power_rows + loco_power_rows
        for row in power_rows:
            candidate = str(row["candidate"]).split(":", 1)[-1]
            route = route_totals[(candidate, str(row["workload"]))]
            row["routed_full_power_delta_nw"] = number(route, "routed_delta_nw")
            row["routed_clock_power_delta_nw"] = number(route, "clock_net_nw")
        total_power_groups: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
        for row in power_rows:
            total_power_groups[(str(row["scope"]), str(row["fold"]), str(row["model"]))].append(row)
        for (scope, fold, model_name), rows in sorted(total_power_groups.items()):
            actual = [number(row, "routed_full_power_delta_nw") for row in rows]
            # Placement-only models do not predict downstream CTS clock-tree changes.
            predicted = [number(row, "predicted_signal_power_delta_nw") for row in rows]
            correct, cases = sign_counts(actual, predicted)
            metrics.append({"scope": scope, "fold": fold, "target": "full routed power incl. CTS clock",
                            "model": model_name, "rows": len(rows), "mae": mae(actual, predicted),
                            "unit": "nW", "sign_matches": correct, "sign_cases": cases})
        write_table(out / "candidate_heldout_metrics.tsv", metrics)
        write_table(out / "candidate_workload_predictions.tsv", power_rows)
        write_table(out / "per_net_predictions.tsv", transfer_net_rows + loco_net_rows)

        report_lines = [
            "# Candidate-held-out placement score evaluation",
            "",
            f"GCD routed-safe layouts: {len(old_groups)} prior training candidates and {len(expanded_groups)} expanded candidates held out or folded; each candidate contains three workloads and 314 common signal nets.",
            f"PicoRV32 training layouts: {len(pico_groups)} candidates with 8,584 common signal nets each.",
            "The strict transfer folds use PicoRV32 plus the older safe GCD layouts for training, then predict all expanded GCD layouts. The GCD LOCO folds train on eight expanded layouts and hold out the ninth layout across every workload.",
            "Timing features are placement-stage setup criticality and slack per net, exported from the candidate placement ODB with the matching 20 ns GCD SDC. A net with no constrained sink is marked unconstrained and receives zero criticality.",
            "",
            "## Candidate/workload routed-power MAE",
            "",
            "| Validation | Target | Model | MAE | Sign agreement |",
            "|---|---|---|---:|---:|",
        ]
        summary: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
        for row in metrics:
            if row["target"] in {"routed signal power", "full routed power incl. CTS clock"}:
                summary[(str(row["scope"]), str(row["target"]), str(row["model"]))].append(row)
        for scope in ("Pico + older GCD → expanded GCD", "expanded GCD LOCO"):
            targets = ("routed signal power", "full routed power incl. CTS clock")
            for target in targets:
                model_names = sorted({model for (s, t, model) in summary if s == scope and t == target})
                for model in model_names:
                    folds = summary[(scope, target, model)]
                    actual_mae = statistics.mean(number(r, "mae") for r in folds)
                    # For LOCO this macro-average gives each held-out layout equal weight.
                    correct = sum(int(r["sign_matches"]) for r in folds)
                    cases = sum(int(r["sign_cases"]) for r in folds)
                    report_lines.append(f"| {scope} | {target} | {model} | {actual_mae:.3f} nW | {correct}/{cases} |")
        report_lines.extend([
            "",
            "## Per-net routed capacitance MAE",
            "",
            "| Validation | Model | MAE | Sign agreement |",
            "|---|---|---:|---:|",
        ])
        for scope in ("Pico + older GCD → expanded GCD", "expanded GCD LOCO"):
            models = sorted({str(r["model"]) for r in metrics if r["scope"] == scope and r["target"] == "routed ΔC"})
            for model in models:
                folds = [r for r in metrics if r["scope"] == scope and r["target"] == "routed ΔC" and r["model"] == model]
                cap_mae = statistics.mean(number(r, "mae") for r in folds)
                correct = sum(int(r["sign_matches"]) for r in folds)
                cases = sum(int(r["sign_cases"]) for r in folds)
                report_lines.append(f"| {scope} | {model} | {cap_mae:.6f} fF | {correct}/{cases} |")
        report_lines.extend([
            "",
            "## Interpretation",
            "",
            "Direct placement ΔC is the no-fit baseline. The transfer result checks whether PicoRV32 and prior GCD data predict the held-out GCD route changes; the LOCO comparison isolates whether placement-timing features help within GCD. The full-power target includes CTS clock-net change, while the placement-only estimate predicts signal-net power and assigns no clock-tree change. Timing is a model feature only; the existing routed 50 ps guard remains a hard eligibility check.",
            "This is a small, selected calibration set. A lower MAE is evidence for ranking experiments, not proof that a learned term will improve a production placer. Detailed-route SPEF remains the acceptance measurement.",
            "",
            "Metrics, candidate/workload predictions, per-net predictions, export hashes, and OpenROAD logs are saved in this directory.",
        ])
        report = out / "candidate_heldout_report.md"
        report.write_text("\n".join(report_lines) + "\n")
        print(report.read_text(), end="")

        source_paths = list(inputs) + [OLD_CAP_MANIFEST, EXP_CAP_MANIFEST]
        for cap_dir in (OLD_CAP_DIR, EXP_CAP_DIR):
            for path in cap_dir.rglob("*.driver_caps.tsv"):
                source_paths.append(path)
        for candidate in old_candidates:
            source_paths.extend((OLD_ODB_DIR / f"{candidate}.odb",))
            for workload in WORKLOADS:
                source_paths.append(ROOT / "build/power" / f"gcd_cal_{candidate}_20261027" / "net_attribution" / f"{workload}.per_net_delta.csv")
        for candidate in expanded_candidates:
            source_paths.append(EXP_ODB_DIR / f"{candidate}.odb")
            for workload in WORKLOADS:
                source_paths.append(ROOT / "build/power" / f"gcd_cal_{candidate}_exp20261027" / "net_attribution" / f"{workload}.per_net_delta.csv")
        source_paths.append(PICO_FEATURES)
        hash_rows = [{"path": str(path), "sha256": sha256(path)} for path in sorted(set(source_paths))]
        write_table(out / "input_hashes.tsv", hash_rows)
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, StopIteration, np.linalg.LinAlgError) as exc:
        print(f"candidate-held-out placement score analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
