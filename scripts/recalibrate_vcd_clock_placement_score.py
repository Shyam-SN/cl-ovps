#!/usr/bin/env python3
"""Recalibrate VCD-weighted signal and placement-based CTS-clock cost models.

Signal models predict routed signal-net delta wire capacitance from placement
features. Training rows are candidate-balanced, with optional additional
within-candidate weighting by the net's aggregate VCD transition density.
Six prospectively routed GCD candidates are held out from fitting.

The separate clock model is deliberately candidate-level: placement ODBs have
no post-CTS clock tree. Its aggregate placement features are evaluated with
leave-one-candidate-out validation before any clock estimate is deployed.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

import analyze_cross_design_placement_score as base


ROOT = Path(__file__).resolve().parents[1]
SCORE_DIR = ROOT / "build/power/gcd_cross_design_score_20261027"
TRIAL_DIR = ROOT / "build/power/gcd_prospective_rank_20261027"
OUT = ROOT / "build/power/gcd_activity_clock_recalibration_20261027"
WORKLOADS = base.WORKLOADS
TERMS = base.CAP_TERMS + base.GEOMETRY_TERMS
POWER_FACTOR = base.POWER_FACTOR


def read_rows(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
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
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_caps(path: Path) -> dict[str, dict[str, float | str]]:
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
        raise ValueError(f"no driver capacitances found in {path}")
    return result


def cached_groups() -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Rebuild historical labels while reusing hash-checked OpenROAD exports."""
    manifest = base.read_table(SCORE_DIR / "export_manifest.tsv", "\t")
    old_candidates = base.route_safe_candidates(base.OLD_ROUTE_VALIDATION)
    expanded_candidates = base.route_safe_candidates(base.EXP_ROUTE_VALIDATION)
    if {r["label"] for r in manifest} != {
        "gcd_baseline20ns", *[f"old_{c}" for c in old_candidates],
        *[f"expanded_{c}" for c in expanded_candidates],
    }:
        raise ValueError("cached physical/timing export set does not match routed-safe data")
    manifest_by_label = {r["label"]: r for r in manifest}

    def cached_export(odb: Path, label: str, _out: Path):
        row = manifest_by_label[label]
        if Path(row["odb"]).resolve() != odb.resolve():
            raise ValueError(f"cached ODB identity mismatch for {label}")
        for key, hash_key in (("physical_csv", "physical_sha256"), ("timing_tsv", "timing_sha256")):
            if sha256(Path(row[key])) != row[hash_key]:
                raise ValueError(f"cached feature hash mismatch: {row[key]}")
        return Path(row["physical_csv"]), Path(row["timing_tsv"]), row

    original = base.export_odb_features
    base.export_odb_features = cached_export
    try:
        gcd, _ = base.build_gcd_groups(OUT, old_candidates, expanded_candidates)
    finally:
        base.export_odb_features = original
    return base.build_pico_groups(), gcd


def make_prospective_groups() -> dict[str, dict[str, object]]:
    """Join the six prospective route labels to placement caps/topology."""
    baseline_caps = read_caps(ROOT / "build/power/gcd_baseline20ns/typical.driver_caps.tsv")
    baseline_geom = base.read_geometry(SCORE_DIR / "physical/gcd_baseline20ns.nets.csv")
    groups = {}
    for candidate in sorted(path.name for path in (TRIAL_DIR / "attribution").iterdir()
                            if path.is_dir()):
        candidate_caps = read_caps(TRIAL_DIR / "placement_caps" / candidate / "typical.driver_caps.tsv")
        candidate_geom = base.read_geometry(TRIAL_DIR / "physical" / f"{candidate}.nets.csv")
        per_net: dict[str, dict[str, object]] = {}
        for workload in WORKLOADS:
            path = TRIAL_DIR / "attribution" / candidate / f"{workload}.per_net_delta.csv"
            for item in read_rows(path):
                if item["baseline_activity_origin"] == "clock" or item["net"].startswith("clknet_"):
                    continue
                net = item["net"]
                pin = item["baseline_driver_pin"]
                if not pin or pin not in baseline_caps or pin not in candidate_caps:
                    raise ValueError(f"prospective cap join failed for {candidate}/{workload}/{pin}")
                if net not in baseline_geom or net not in candidate_geom:
                    raise ValueError(f"prospective geometry join failed for {candidate}/{net}")
                bc, cc = baseline_caps[pin], candidate_caps[pin]
                if str(bc["net"]) != net or str(cc["net"]) != net:
                    raise ValueError(f"prospective driver/net mismatch for {candidate}/{pin}")
                bg, cg = baseline_geom[net], candidate_geom[net]
                features = {
                    "delta_place_wire_cap_ff": (float(cc["wire_cap"]) - float(bc["wire_cap"])) * 1e15,
                    "delta_place_load_cap_ff": (float(cc["total_cap"]) - float(bc["total_cap"])) * 1e15,
                    "place_delta_hpwl_um": cg["hpwl_um"] - bg["hpwl_um"],
                    "place_abs_delta_hpwl_um": abs(cg["hpwl_um"] - bg["hpwl_um"]),
                    "place_delta_bbox_width_um": cg["bbox_width_um"] - bg["bbox_width_um"],
                    "place_delta_bbox_height_um": cg["bbox_height_um"] - bg["bbox_height_um"],
                    "place_baseline_fanout": bg["fanout"],
                    "place_delta_fanout": cg["fanout"] - bg["fanout"],
                    "place_delta_endpoint_count": cg["endpoint_count"] - bg["endpoint_count"],
                    "place_baseline_hpwl_um": bg["hpwl_um"],
                    "place_baseline_endpoint_count": bg["endpoint_count"],
                }
                target_cap = base.number(item, "delta_wire_cap_f") * 1e15
                record = per_net.setdefault(net, {"candidate": f"prospective:{candidate}",
                    "source": "prospective", "net": net, "features": features,
                    "target_cap_ff": target_cap, "workloads": {}})
                if not base.close(float(record["target_cap_ff"]), target_cap):
                    raise ValueError(f"route ΔC changes across workloads for {candidate}/{net}")
                record["workloads"][workload] = {
                    "density_hz": base.number(item, "baseline_density_hz"),
                    "routed_power_w": base.number(item, "delta_switching_power_w"),
                }
        if len(per_net) != 314 or any(set(r["workloads"]) != set(WORKLOADS) for r in per_net.values()):
            raise ValueError(f"expected 314 common signal nets with three workloads for {candidate}")
        groups[f"prospective:{candidate}"] = {"family": "prospective", "candidate": candidate,
                                               "per_net": per_net}
    if len(groups) != 6:
        raise ValueError(f"expected six prospective holdouts, got {len(groups)}")
    return groups


def activity_weights(rows: list[dict[str, object]], gamma: float) -> np.ndarray:
    """Give each placement equal total weight; weight its nets by VCD activity^gamma."""
    by_candidate: dict[str, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        by_candidate[str(row["candidate"])].append(i)
    result = np.zeros(len(rows), dtype=float)
    for indices in by_candidate.values():
        raw = []
        for i in indices:
            workloads = rows[i]["workloads"]
            activity = sum(max(float(workloads[w]["density_hz"]), 0.0) for w in WORKLOADS)
            raw.append(max(activity, 1e-12) ** gamma)
        total = sum(raw)
        for i, value in zip(indices, raw):
            result[i] = value / total
    return result


def matrix(rows: list[dict[str, object]]) -> np.ndarray:
    return np.asarray([[float(r["features"].get(t, 0.0)) for t in TERMS] for r in rows], dtype=float)


def power_summary(groups: dict[str, dict[str, object]], predictions: dict[str, np.ndarray],
                  rows: list[dict[str, object]]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    candidate_workload: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"actual": 0.0})
    per_net = []
    for candidate, group in groups.items():
        for i, (net, record) in enumerate(group["per_net"].items()):
            pred_cap = float(predictions[candidate][i])
            for workload, observed in record["workloads"].items():
                actual = float(observed["routed_power_w"])
                predicted = POWER_FACTOR * float(observed["density_hz"]) * pred_cap * 1e-15
                key = (candidate.split(":", 1)[-1], workload)
                candidate_workload[key]["actual"] += actual
                candidate_workload[key]["predicted"] = candidate_workload[key].get("predicted", 0.0) + predicted
                per_net.append({"candidate": candidate.split(":", 1)[-1], "net": net,
                    "workload": workload, "predicted_delta_wire_cap_ff": pred_cap,
                    "actual_delta_wire_cap_ff": float(record["target_cap_ff"]),
                    "vcd_density_hz": float(observed["density_hz"]),
                    "predicted_signal_delta_nw": predicted * 1e9, "actual_signal_delta_nw": actual * 1e9})
    summary = [{"candidate": c, "workload": w, "actual_signal_delta_nw": v["actual"] * 1e9,
                "predicted_signal_delta_nw": v["predicted"] * 1e9,
                "absolute_error_nw": abs(v["actual"] - v["predicted"]) * 1e9,
                "sign_match": int(v["actual"] * v["predicted"] > 0)}
               for (c, w), v in sorted(candidate_workload.items())]
    return summary, per_net


def rankdata(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    pos = 0
    while pos < len(order):
        end = pos + 1
        while end < len(order) and values[order[end]] == values[order[pos]]:
            end += 1
        rank = (pos + 1 + end) / 2
        for j in range(pos, end):
            ranks[order[j]] = rank
        pos = end
    return ranks


def spearman(x: list[float], y: list[float]) -> float:
    if len(x) < 2 or len(x) != len(y):
        return float("nan")
    rx, ry = rankdata(x), rankdata(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def clock_features(group: dict[str, object]) -> dict[str, float]:
    records = list(group["per_net"].values())
    return {
        "sum_abs_place_wire_cap_delta_ff": sum(abs(float(r["features"]["delta_place_wire_cap_ff"])) for r in records),
        "sum_signed_place_wire_cap_delta_ff": sum(float(r["features"]["delta_place_wire_cap_ff"]) for r in records),
        "sum_abs_hpwl_delta_um": sum(float(r["features"]["place_abs_delta_hpwl_um"]) for r in records),
        "sum_signed_hpwl_delta_um": sum(float(r["features"]["place_delta_hpwl_um"]) for r in records),
        "max_abs_hpwl_delta_um": max(float(r["features"]["place_abs_delta_hpwl_um"]) for r in records),
    }


def clock_labels() -> dict[str, float]:
    result: dict[str, list[float]] = defaultdict(list)
    sources = (("old_gcd", base.OLD_ROUTE_VALIDATION), ("expanded_gcd", base.EXP_ROUTE_VALIDATION))
    for family, path in sources:
        allowed = set(base.route_safe_candidates(path))
        for row in base.read_table(path, "\t"):
            if row["pair_id"] in allowed:
                result[f"{family}:{row['pair_id']}"].append(base.number(row, "clock_net_nw"))
    return {candidate: statistics.mean(values) for candidate, values in result.items()
            if len(values) == len(WORKLOADS)}


def clock_cv(gcd_groups: dict[str, dict[str, object]], labels: dict[str, float]) -> tuple[list[dict[str, object]], dict[str, object]]:
    candidates = []
    for group_id, group in gcd_groups.items():
        label_key = f"{group['family']}:{group['candidate']}"
        if label_key not in labels:
            continue
        candidates.append({"group_id": group_id, "candidate": label_key,
                           "features": clock_features(group), "target": labels[label_key]})
    feature_names = list(clock_features(next(iter(gcd_groups.values()))))
    folds = []
    for held in candidates:
        train = [r for r in candidates if r is not held]
        x_train = np.asarray([[r["features"][k] for k in feature_names] for r in train], dtype=float)
        y_train = np.asarray([r["target"] for r in train], dtype=float)
        x_test = np.asarray([[held["features"][k] for k in feature_names]], dtype=float)
        mean = float(np.mean(y_train))
        fit = base.fit_ridge(x_train, y_train, np.ones(len(y_train)))
        pred = float(base.ridge_predict(x_test, fit)[0])
        folds.append({"candidate": held["candidate"], "actual_clock_delta_nw": held["target"],
                      "leave_one_out_mean_nw": mean, "predicted_clock_delta_nw": pred,
                      "ridge_abs_error_nw": abs(pred - held["target"]),
                      "mean_abs_error_nw": abs(mean - held["target"]),
                      "sign_match_ridge": int(pred * held["target"] > 0),
                      "sign_match_mean": int(mean * held["target"] > 0)})
    report = {
        "candidates": len(folds),
        "mean_baseline_mae_nw": statistics.mean(float(r["mean_abs_error_nw"]) for r in folds),
        "ridge_mae_nw": statistics.mean(float(r["ridge_abs_error_nw"]) for r in folds),
        "mean_baseline_sign_matches": sum(int(r["sign_match_mean"]) for r in folds),
        "ridge_sign_matches": sum(int(r["sign_match_ridge"]) for r in folds),
        "candidate_level_spearman": spearman([float(r["predicted_clock_delta_nw"]) for r in folds],
                                               [float(r["actual_clock_delta_nw"]) for r in folds]),
        "feature_names": feature_names,
    }
    return folds, report


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    pico, gcd = cached_groups()
    train_groups = {**pico, **gcd}
    test_groups = make_prospective_groups()
    train_rows, test_rows = base.flatten_groups(train_groups), base.flatten_groups(test_groups)
    train_x, train_y, test_x = matrix(train_rows), np.asarray([float(r["target_cap_ff"]) for r in train_rows]), matrix(test_rows)
    test_predictions = {}
    model_descriptors = {}
    all_power_rows, all_net_rows, metrics = [], [], []
    prediction_index = {(str(r["candidate"]), str(r["net"])): i for i, r in enumerate(test_rows)}
    for gamma in (0.0, 0.5, 1.0):
        weights = activity_weights(train_rows, gamma)
        fitted = base.fit_ridge(train_x, train_y, weights)
        predicted = base.ridge_predict(test_x, fitted)
        model_name = f"activity_gamma_{gamma:g}"
        group_predictions = {}
        offset = 0
        # test_rows are flattened in group insertion order, preserving contiguous candidates.
        for candidate, group in test_groups.items():
            count = len(group["per_net"])
            group_predictions[candidate] = predicted[offset:offset + count]
            offset += count
        test_predictions[model_name] = group_predictions
        summary, nets = power_summary(test_groups, group_predictions, test_rows)
        all_power_rows.extend([{"model": model_name, **row} for row in summary])
        all_net_rows.extend([{"model": model_name, **row} for row in nets])
        errors = [float(r["absolute_error_nw"]) for r in summary]
        actual_by_candidate: dict[str, list[float]] = defaultdict(list)
        pred_by_candidate: dict[str, list[float]] = defaultdict(list)
        for row in summary:
            actual_by_candidate[str(row["candidate"])].append(float(row["actual_signal_delta_nw"]))
            pred_by_candidate[str(row["candidate"])].append(float(row["predicted_signal_delta_nw"]))
        actual_worst = [max(v) for _, v in sorted(actual_by_candidate.items())]
        pred_worst = [max(pred_by_candidate[c]) for c in sorted(pred_by_candidate)]
        metrics.append({"model": model_name, "activity_gamma": gamma,
            "holdout_candidate_workload_mae_nw": statistics.mean(errors),
            "holdout_candidate_worst_workload_mae_nw": statistics.mean(abs(a - p) for a, p in zip(actual_worst, pred_worst)),
            "holdout_worst_workload_rank_spearman": spearman(pred_worst, actual_worst),
            "holdout_direction_matches": sum(int(r["sign_match"]) for r in summary),
            "holdout_direction_cases": len(summary), "holdout_layout_count": len(test_groups)})
        model_data = {
            "model": "ridge_cap_geometry_vcd_weighted",
            "target": "routed_signal_delta_wire_cap_ff",
            "alpha": base.ALPHA,
            "activity_gamma": gamma,
            "terms": list(TERMS),
            "weighted_feature_mean": fitted[0].tolist(),
            "weighted_feature_scale": fitted[1].tolist(),
            "standardized_coefficients": fitted[2].tolist(),
            "intercept_ff": fitted[3],
            "weighting": "candidate-balanced; within each candidate, each net weight is proportional to the sum of idle, typical, and peak VCD transition densities raised to activity_gamma",
            "training_candidates": sorted(train_groups),
            "training_candidate_count": len(train_groups),
            "prospective_holdout_candidates": sorted(test_groups),
            "note": "Predicts signal-net routed delta capacitance only; CTS clock delta must be handled separately.",
        }
        model_path = OUT / f"{model_name}.json"
        model_path.write_text(json.dumps(model_data, indent=2) + "\n")
        model_descriptors[model_name] = {"path": str(model_path), "sha256": sha256(model_path)}

    # Compare activity exponents only on the six candidates excluded from fitting.
    metrics_sorted = sorted(metrics, key=lambda r: float(r["holdout_candidate_workload_mae_nw"]))
    selected = metrics_sorted[0]

    # Clock: global pre-CTS clk HPWL/fanout are invariant across this candidate set.
    # Test whether aggregate signal-placement disturbance offers any useful proxy.
    labels = clock_labels()
    clock_folds, clock_report = clock_cv(gcd, labels)
    write_rows(OUT / "signal_holdout_predictions.tsv", all_power_rows)
    write_rows(OUT / "signal_holdout_per_net.tsv", all_net_rows)
    write_rows(OUT / "signal_model_metrics.tsv", metrics)
    write_rows(OUT / "clock_loco_predictions.tsv", clock_folds)
    clock_test_rows = []
    clock_model_rows = [r for r in gcd.values() if f"{r['family']}:{r['candidate']}" in labels]
    names = clock_report["feature_names"]
    if len(clock_model_rows) != len(labels):
        raise ValueError("clock labels and routed-safe GCD feature candidates do not align")
    x_clock = np.asarray([[clock_features(g)[n] for n in names] for g in clock_model_rows], dtype=float)
    y_clock = np.asarray([labels[f"{g['family']}:{g['candidate']}"] for g in clock_model_rows], dtype=float)
    clock_fit = base.fit_ridge(x_clock, y_clock, np.ones(len(y_clock)))
    for c, group in test_groups.items():
        feature = clock_features(group)
        x = np.asarray([[feature[n] for n in names]], dtype=float)
        clock_test_rows.append({"candidate": c.split(":", 1)[-1], "predicted_clock_delta_nw": float(base.ridge_predict(x, clock_fit)[0]),
                               "training_mean_clock_delta_nw": float(np.mean(y_clock)), **feature})
    write_rows(OUT / "clock_prospective_estimates.tsv", clock_test_rows)

    markdown = [
        "# VCD-weighted signal and placement clock-cost recalibration",
        "",
        "## Signal cost model",
        "",
        "The signal objective remains a workload-specific VCD-weighted sum:",
        "`ΔP_signal(w) = 0.5 × V² × Σ_n density(n,w) × predicted_ΔC_routed(n)`. The experiment changes how the capacitance model is fit: gamma 0 is uniform within each candidate, gamma 0.5 uses square-root activity weighting, and gamma 1 uses linear aggregate activity weighting. Each training placement still has equal total weight.",
        "",
        "Six prospectively routed GCD candidates were excluded from model fitting. The rows below report candidate/workload signal-power prediction error and candidate worst-workload ranking.",
        "",
        "| Model | Workload-case MAE (nW) | Worst-workload MAE (nW) | Worst-case rank ρ | Direction matches |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in metrics:
        markdown.append(f"| {row['model']} | {float(row['holdout_candidate_workload_mae_nw']):.2f} | {float(row['holdout_candidate_worst_workload_mae_nw']):.2f} | {float(row['holdout_worst_workload_rank_spearman']):.3f} | {row['holdout_direction_matches']}/{row['holdout_direction_cases']} |")
    markdown.extend([
        "",
        f"Selected for the next ranking experiment by held-out candidate/workload MAE: **{selected['model']}**. This is a small prospective sample (six layouts), so it is only a ranking-model selection signal; detailed-route SPEF remains the acceptance measurement.",
        "All six held-out candidates increased signal power in all three workloads, so the 18/18 direction matches do not demonstrate that the model can distinguish improvements from regressions. Candidate ordering also did not change across the three activity exponents.",
        "",
        "## Clock cost visibility",
        "",
        "The placement-stage global `clk` HPWL/fanout/endpoint count are invariant across the candidate ODBs. The clock experiment therefore tests aggregate signal placement disturbance features in candidate-held-out folds; it does not pretend that the pre-CTS `clk` net is the CTS tree.",
        f"LOCO candidates: {clock_report['candidates']}; training-mean baseline MAE: {clock_report['mean_baseline_mae_nw']:.2f} nW; aggregate-placement ridge MAE: {clock_report['ridge_mae_nw']:.2f} nW; sign matches mean/ridge: {clock_report['mean_baseline_sign_matches']}/{clock_report['candidates']} vs {clock_report['ridge_sign_matches']}/{clock_report['candidates']}; prediction/label rank ρ: {clock_report['candidate_level_spearman']:.3f}.",
        "",
        "Clock estimate deployment rule: use the placement-based clock ridge only if LOCO beats the candidate-mean baseline in both MAE and sign accuracy. Otherwise give no predicted clock-saving credit and require CTS/route evaluation before claiming a total-power win.",
        "",
        "## Result files",
        "",
        "- `signal_model_metrics.tsv`: held-out comparison of the three activity exponents.",
        "- `signal_holdout_predictions.tsv` and `signal_holdout_per_net.tsv`: candidate/workload and net-level validation results.",
        "- `clock_loco_predictions.tsv`: candidate-held-out clock proxy results.",
        "- `clock_prospective_estimates.tsv`: separately reported clock estimates for the six prospective layouts.",
        "- `activity_gamma_*.json`: fitted signal capacitance models, with their VCD weighting definition.",
        "",
        "The train/test source hashes are recorded in `input_hashes.tsv`.",
    ])
    (OUT / "recalibration_report.md").write_text("\n".join(markdown) + "\n")

    input_paths = [SCORE_DIR / "export_manifest.tsv", base.PICO_FEATURES, base.OLD_ROUTE_VALIDATION,
                   base.EXP_ROUTE_VALIDATION, base.OLD_CAP_MANIFEST, base.EXP_CAP_MANIFEST]
    prospective_ids = [c.split(":", 1)[-1] for c in test_groups]
    input_paths += [TRIAL_DIR / "physical" / f"{c}.nets.csv" for c in prospective_ids]
    input_paths += [TRIAL_DIR / "placement_caps" / c / "typical.driver_caps.tsv" for c in prospective_ids]
    input_paths += [TRIAL_DIR / "attribution" / c / f"{w}.per_net_delta.csv" for c in prospective_ids for w in WORKLOADS]
    write_rows(OUT / "input_hashes.tsv", [{"path": str(p), "sha256": sha256(p)} for p in sorted(set(input_paths))])
    print((OUT / "recalibration_report.md").read_text(), end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
        print(f"VCD/clock score recalibration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
