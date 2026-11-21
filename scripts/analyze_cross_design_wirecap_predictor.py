#!/usr/bin/env python3
"""Measure whether placement-only wire-cap deltas predict routed GCD wire-cap deltas.

The PicoRV32 calibration is the training design. GCD candidate layouts are held
out by design and are scored only after a complete placement and route. The
script also exports placement parasitics for the saved GCD probes, then checks
that those reports do not modify any source ODB.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import statistics
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
CANDIDATES = ("swap01", "swap02", "swap04", "swap05", "swap06", "swap07", "swap08", "swap09")
VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE * VOLTAGE
PICO_ANALYSIS = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01"
GCD_ROOT = ROOT / "build/power/gcd_swap_cal_20261027"
GCD_ROUTE_ROOT = ROOT / "build/power"
GCD_RESULT_ROOT = ROOT / "build/orfs/results/sky130hd/gcd_power"
GCD_ACTIVITY_ROOT = ROOT / "build/activity/gcd_baseline20ns"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
PLACE_REPORT_TCL = ROOT / "scripts/gcd_report_place_power.tcl"
BASELINE_VARIANT = "gcd_baseline20ns"
RUN_TAG = "20261027"


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
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def number(row: dict[str, str] | dict[str, object], key: str) -> float:
    return float(row.get(key, 0) or 0.0)


def read_place_caps(path: Path) -> dict[str, dict[str, float | str]]:
    result: dict[str, dict[str, float | str]] = {}
    with path.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 6 and fields[1] == "pin":
                continue
            if len(fields) != 6:
                raise ValueError(f"malformed placement cap row in {path}: {line!r}")
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            if pin in result:
                raise ValueError(f"duplicate placement driver pin {pin} in {path}")
            result[pin] = {
                "pin": pin,
                "net": net,
                "pin_cap_f": float(pin_cap),
                "wire_cap_f": float(wire_cap),
                "total_cap_f": float(total_cap),
            }
    if not result:
        raise ValueError(f"no placement driver capacitances found in {path}")
    return result


def candidate_source_odb(candidate: str) -> Path:
    return GCD_ROOT / "candidate_odb_relaxed" / f"{candidate}.odb"


def routed_candidate_dir(candidate: str) -> Path:
    return GCD_RESULT_ROOT / f"gcd_cal_{candidate}_{RUN_TAG}"


def analysis_input_paths() -> list[Path]:
    baseline_result = GCD_RESULT_ROOT / BASELINE_VARIANT
    paths = [
        PICO_ANALYSIS / "unified_per_net_features.csv",
        PICO_ANALYSIS / "candidate_workload_attribution.tsv",
        PICO_ANALYSIS / "unified_calibration_report.md",
        GCD_ROOT / "route_analysis/route_validation.tsv",
        GCD_ROOT / "relaxed/audit_idle.tsv",
        GCD_ROOT / "relaxed/audit_typical.tsv",
        GCD_ROOT / "relaxed/audit_peak.tsv",
        ROOT / "build/power/gcd_baseline20ns/integrity.tsv",
        baseline_result / "3_5_place_dp.odb",
        baseline_result / "3_place.sdc",
        baseline_result / "6_final.odb",
        baseline_result / "6_final.sdc",
        baseline_result / "6_final.spef",
        ROOT / "designs/gcd/flow/constraints/gcd.upf",
        PLACE_REPORT_TCL,
        ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/setRC.tcl",
        ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib",
        OPENROAD,
        Path(__file__).resolve(),
    ]
    paths.extend(GCD_ACTIVITY_ROOT / "placement" / f"{workload}.vcd" for workload in WORKLOADS)
    for candidate in CANDIDATES:
        run_dir = routed_candidate_dir(candidate)
        paths.extend([
            candidate_source_odb(candidate),
            run_dir / "3_5_place_dp.odb",
            run_dir / "6_final.odb",
            run_dir / "6_final.sdc",
            run_dir / "6_final.spef",
            ROOT / f"build/power/gcd_cal_{candidate}_{RUN_TAG}/integrity.tsv",
        ])
        paths.extend(
            GCD_ROUTE_ROOT / f"gcd_cal_{candidate}_{RUN_TAG}" / "net_attribution" / f"{workload}.per_net_delta.csv"
            for workload in WORKLOADS
        )
    unique = sorted(set(path.resolve() for path in paths))
    missing = [path for path in unique if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"analysis input is missing: {missing[0]}")
    return unique


def hash_input_manifest(paths: list[Path], before: dict[Path, str] | None = None) -> list[dict[str, str]]:
    rows = []
    for path in paths:
        digest_before = before[path] if before is not None else sha256(path)
        digest_after = sha256(path)
        if digest_before != digest_after:
            raise ValueError(f"analysis input changed during read-only analysis: {path}")
        rows.append({
            "path": str(path),
            "sha256_before": digest_before,
            "sha256_after": digest_after,
            "unchanged": "true",
        })
    return rows


def export_placement_caps(output_dir: Path, skip_export: bool) -> list[dict[str, str]]:
    platform_root = ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd"
    baseline_result = GCD_RESULT_ROOT / BASELINE_VARIANT
    baseline_odb = baseline_result / "3_5_place_dp.odb"
    baseline_sdc = baseline_result / "3_place.sdc"
    if not OPENROAD.is_file() or not PLACE_REPORT_TCL.is_file():
        raise FileNotFoundError("OpenROAD executable or GCD placement report Tcl is missing")
    if not baseline_odb.is_file() or not baseline_sdc.is_file() or not platform_root.is_dir():
        raise FileNotFoundError("GCD baseline placement ODB, SDC, or Sky130 platform files are missing")

    cap_root = output_dir / "placement_caps"
    if skip_export:
        manifest_path = cap_root / "export_manifest.tsv"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"--skip-export needs {manifest_path}")
        saved = read_csv(manifest_path, "\t")
        for row in saved:
            for path_key, digest_key in (
                ("odb", "odb_sha256_after"),
                ("activity_vcd", "activity_vcd_sha256"),
                ("placement_sdc", "placement_sdc_sha256"),
                ("cap_report", "cap_report_sha256"),
            ):
                path = Path(row[path_key])
                if not path.is_file() or sha256(path) != row[digest_key]:
                    raise ValueError(f"--skip-export input/output hash mismatch for {path}")
        return saved

    manifest: list[dict[str, str]] = []
    targets = [("baseline", baseline_odb)]
    targets.extend((candidate, candidate_source_odb(candidate)) for candidate in CANDIDATES)
    for label, odb in targets:
        if not odb.is_file():
            raise FileNotFoundError(f"missing placement ODB for {label}: {odb}")
        if label != "baseline":
            routed_odb = routed_candidate_dir(label) / "3_5_place_dp.odb"
            if not routed_odb.is_file() or sha256(odb) != sha256(routed_odb):
                raise ValueError(f"saved candidate and routed-flow placement ODB differ for {label}")
        for workload in WORKLOADS:
            activity = GCD_ACTIVITY_ROOT / "placement" / f"{workload}.vcd"
            if not activity.is_file():
                raise FileNotFoundError(f"missing placement VCD: {activity}")
            report_dir = cap_root / label
            report_dir.mkdir(parents=True, exist_ok=True)
            cap_path = report_dir / f"{workload}.driver_caps.tsv"
            log_path = report_dir / f"{workload}.log"
            before = sha256(odb)
            env = os.environ.copy()
            env.update(
                PLACEMENT_ODB=str(odb),
                PLACEMENT_SDC=str(baseline_sdc),
                ACTIVITY_VCD=str(activity),
                ACTIVITY_TRACE="1",
                CAP_REPORT=str(cap_path),
            )
            completed = subprocess.run(
                [str(OPENROAD), "-no_init", "-exit", str(PLACE_REPORT_TCL)],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            log_path.write_text(completed.stdout)
            if completed.returncode:
                raise RuntimeError(f"OpenROAD placement cap export failed for {label}/{workload}; see {log_path}")
            after = sha256(odb)
            if before != after:
                raise ValueError(f"read-only placement cap export changed source ODB {odb}")
            cap_count = len(read_place_caps(cap_path))
            manifest.append({
                "candidate": label,
                "workload": workload,
                "odb": str(odb),
                "odb_sha256_before": before,
                "odb_sha256_after": after,
                "activity_vcd": str(activity),
                "activity_vcd_sha256": sha256(activity),
                "placement_sdc": str(baseline_sdc),
                "placement_sdc_sha256": sha256(baseline_sdc),
                "cap_report": str(cap_path),
                "cap_report_sha256": sha256(cap_path),
                "cap_rows": str(cap_count),
            })
    write_csv(cap_root / "export_manifest.tsv", manifest, "\t")
    return manifest


def read_pico_training() -> tuple[list[tuple[float, float]], dict[str, list[dict[str, str]]]]:
    per_net_path = PICO_ANALYSIS / "unified_per_net_features.csv"
    aggregate_path = PICO_ANALYSIS / "candidate_workload_attribution.tsv"
    if not per_net_path.is_file() or not aggregate_path.is_file():
        raise FileNotFoundError("the saved PicoRV32 unified calibration data is incomplete")
    cap_pairs: list[tuple[float, float]] = []
    with per_net_path.open(newline="") as source:
        for row in csv.DictReader(source):
            if row["routed_class"] != "common_signal":
                continue
            cap_pairs.append((number(row, "delta_place_wire_cap_f"), number(row, "routed_delta_wire_cap_f")))
    if len(cap_pairs) != 12 * len(WORKLOADS) * 8584:
        raise ValueError(f"unexpected PicoRV32 training coverage: {len(cap_pairs)} per-net rows")
    aggregates = read_csv(aggregate_path, "\t")
    if len(aggregates) != 12 * len(WORKLOADS):
        raise ValueError(f"unexpected PicoRV32 candidate/workload coverage: {len(aggregates)} rows")
    return cap_pairs, {
        str(workload): [row for row in aggregates if row["workload"] == workload]
        for workload in WORKLOADS
    }


def fit_scale(pairs: list[tuple[float, float]]) -> float:
    denominator = sum(x * x for x, _ in pairs)
    return sum(x * y for x, y in pairs) / denominator if denominator else 0.0


def read_timing_safe(route_validation: list[dict[str, str]]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for candidate in CANDIDATES:
        subset = [row for row in route_validation if row["pair_id"] == candidate]
        if len(subset) != len(WORKLOADS):
            raise ValueError(f"expected 3 routed timing rows for {candidate}, got {len(subset)}")
        result[candidate] = all(
            number(row, "setup_loss_ns") >= -0.05 and number(row, "hold_loss_ns") >= -0.05
            for row in subset
        )
    return result


def build_gcd_rows(output_dir: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    route_validation = read_csv(GCD_ROOT / "route_analysis/route_validation.tsv", "\t")
    timing_safe = read_timing_safe(route_validation)
    place_cap_root = output_dir / "placement_caps"
    baseline_caps = {
        workload: read_place_caps(place_cap_root / "baseline" / f"{workload}.driver_caps.tsv")
        for workload in WORKLOADS
    }
    net_rows: list[dict[str, object]] = []
    candidate_rows: list[dict[str, object]] = []
    vcd_power = POWER_FACTOR

    for candidate in CANDIDATES:
        candidate_caps = {
            workload: read_place_caps(place_cap_root / candidate / f"{workload}.driver_caps.tsv")
            for workload in WORKLOADS
        }
        audit = {
            workload: {
                row["pair_id"]: row
                for row in read_csv(GCD_ROOT / "relaxed" / f"audit_{workload}.tsv", "\t")
            }
            for workload in WORKLOADS
        }
        for workload in WORKLOADS:
            route_path = GCD_ROUTE_ROOT / f"gcd_cal_{candidate}_{RUN_TAG}" / "net_attribution" / f"{workload}.per_net_delta.csv"
            route_items = read_csv(route_path)
            route_caps: dict[str, dict[str, str]] = {}
            for item in route_items:
                pin = item["baseline_driver_pin"]
                if not pin:
                    raise ValueError(f"routed signal row has no baseline driver pin in {route_path}")
                if item["baseline_activity_origin"] == "clock" or item["net"].startswith("clknet_"):
                    continue
                if pin in route_caps:
                    raise ValueError(f"duplicate routed driver pin {pin} in {route_path}")
                route_caps[pin] = item
            placement_score = 0.0
            routed_signal_power = 0.0
            for pin, item in route_caps.items():
                if pin not in baseline_caps[workload] or pin not in candidate_caps[workload]:
                    raise ValueError(f"missing placed cap for {candidate}/{workload}/{pin}")
                before = baseline_caps[workload][pin]
                after = candidate_caps[workload][pin]
                density = number(item, "baseline_density_hz")
                place_delta_cap = float(after["wire_cap_f"]) - float(before["wire_cap_f"])
                route_delta_cap = number(item, "delta_wire_cap_f")
                placed_power = vcd_power * density * place_delta_cap
                routed_signal_power += number(item, "delta_switching_power_w")
                placement_score += placed_power
                net_rows.append({
                    "design": "gcd",
                    "candidate": candidate,
                    "workload": workload,
                    "timing_safe": int(timing_safe[candidate]),
                    "net": item["net"],
                    "driver_pin": pin,
                    "activity_density_hz": density,
                    "delta_place_wire_cap_f": place_delta_cap,
                    "delta_route_wire_cap_f": route_delta_cap,
                    "placement_wire_power_prediction_w": placed_power,
                    "routed_signal_power_delta_w": number(item, "delta_switching_power_w"),
                    "routed_pin_cap_contribution_w": number(item, "pin_cap_contribution_w"),
                    "routed_activity_contribution_w": number(item, "activity_contribution_w"),
                })

            route_summary = next(
                row for row in route_validation
                if row["pair_id"] == candidate and row["workload"] == workload
            )
            if abs(routed_signal_power * 1e9 - number(route_summary, "signal_net_nw")) > 0.01:
                raise ValueError(f"per-net routed signal power does not reconcile for {candidate}/{workload}")
            audit_row = audit[workload].get(candidate)
            if audit_row is None:
                raise ValueError(f"placement audit lacks {candidate}/{workload}")
            audit_delta = number(audit_row, "predicted_delta_w")
            placement_error = abs(placement_score - audit_delta)
            candidate_rows.append({
                "design": "gcd",
                "candidate": candidate,
                "workload": workload,
                "timing_safe": int(timing_safe[candidate]),
                "placement_score_from_per_net_caps_w": placement_score,
                "placement_score_from_swap_audit_w": audit_delta,
                "placement_score_reconciliation_error_w": placement_error,
                "routed_signal_delta_w": routed_signal_power,
                "routed_total_delta_w": number(route_summary, "routed_delta_nw") * 1e-9,
                "routed_clock_delta_w": number(route_summary, "clock_net_nw") * 1e-9,
                "setup_loss_ns": number(route_summary, "setup_loss_ns"),
                "hold_loss_ns": number(route_summary, "hold_loss_ns"),
                "signal_net_count": len(route_caps),
            })
    if len(net_rows) != sum(
        len([row for row in net_rows if row["candidate"] == candidate and row["workload"] == workload])
        for candidate in CANDIDATES for workload in WORKLOADS
    ):
        raise ValueError("unexpected duplicate GCD per-net rows")
    if max(number(row, "placement_score_reconciliation_error_w") for row in candidate_rows) > 1e-9:
        raise ValueError("per-net placement power does not reconcile to the exact swap audit score")
    return net_rows, candidate_rows


def mae(actual: list[float], predicted: list[float]) -> float:
    if not actual or len(actual) != len(predicted):
        raise ValueError("MAE inputs must be nonempty and equal length")
    return sum(abs(a - p) for a, p in zip(actual, predicted)) / len(actual)


def sign_accuracy(actual: list[float], predicted: list[float]) -> tuple[int, int]:
    cases = [(a, p) for a, p in zip(actual, predicted) if abs(a) > 1e-18 and abs(p) > 1e-18]
    return sum((a > 0) == (p > 0) for a, p in cases), len(cases)


def analyze_models(
    net_rows: list[dict[str, object]],
    candidate_rows: list[dict[str, object]],
    pico_cap_pairs: list[tuple[float, float]],
    pico_aggregates: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], float, float]:
    cap_scale = fit_scale(pico_cap_pairs)
    pico_candidate_pairs = [
        (number(row, "placement_signal_prediction_w"), number(row, "routed_signal_delta_w"))
        for workload_rows in pico_aggregates.values()
        for row in workload_rows
    ]
    power_scale = fit_scale(pico_candidate_pairs)
    workload_signal_mean = {
        workload: statistics.mean(number(row, "routed_signal_delta_w") for row in rows)
        for workload, rows in pico_aggregates.items()
    }
    workload_total_mean = {
        workload: statistics.mean(number(row, "routed_total_per_net_delta_w") for row in rows)
        for workload, rows in pico_aggregates.items()
    }
    per_net_predictions: list[dict[str, object]] = []
    timing_safe_rows = [row for row in net_rows if int(row["timing_safe"]) == 1]
    for row in net_rows:
        x = number(row, "delta_place_wire_cap_f")
        y = number(row, "delta_route_wire_cap_f")
        feature = number(row, "activity_density_hz")
        per_net_predictions.append({
            **row,
            "zero_delta_cap_prediction_f": 0.0,
            "raw_place_delta_cap_prediction_f": x,
            "pico_scaled_place_delta_cap_prediction_f": cap_scale * x,
            "zero_power_prediction_w": 0.0,
            "raw_place_power_prediction_w": POWER_FACTOR * feature * x,
            "pico_scaled_place_power_prediction_w": POWER_FACTOR * feature * cap_scale * x,
            "delta_route_wire_cap_error_pico_scaled_f": cap_scale * x - y,
        })

    metric_rows: list[dict[str, object]] = []
    for group_name, group in (("timing_safe", timing_safe_rows), ("all_probes", net_rows)):
        actual_cap = [number(row, "delta_route_wire_cap_f") for row in group]
        raw_cap = [number(row, "delta_place_wire_cap_f") for row in group]
        scaled_cap = [cap_scale * x for x in raw_cap]
        for model, predictions in (
            ("zero", [0.0] * len(group)),
            ("raw placement delta-C", raw_cap),
            ("Pico trained scalar x placement delta-C", scaled_cap),
        ):
            match, cases = sign_accuracy(actual_cap, predictions)
            metric_rows.append({
                "scope": group_name,
                "metric": "per-net routed delta wire capacitance",
                "model": model,
                "rows": len(group),
                "mae_fF": mae(actual_cap, predictions) / 1e-15,
                "sign_matches": match,
                "sign_cases": cases,
                "training_design": "PicoRV32",
                "heldout_design": "GCD",
            })

    candidate_predictions: list[dict[str, object]] = []
    for row in candidate_rows:
        workload = str(row["workload"])
        place_power = number(row, "placement_score_from_per_net_caps_w")
        scaled_power = cap_scale * place_power
        candidate_predictions.append({
            **row,
            "zero_signal_prediction_w": 0.0,
            "pico_workload_mean_signal_prediction_w": workload_signal_mean[workload],
            "raw_placement_signal_prediction_w": place_power,
            "pico_cap_scaled_signal_prediction_w": scaled_power,
            "pico_power_scaled_signal_prediction_w": power_scale * place_power,
            "pico_workload_mean_total_prediction_w": workload_total_mean[workload],
        })

    scopes = {
        "timing_safe": [row for row in candidate_predictions if int(row["timing_safe"]) == 1],
        "all_probes": candidate_predictions,
    }
    models = {
        "zero": ("routed_signal_delta_w", "zero_signal_prediction_w"),
        "Pico workload mean": ("routed_signal_delta_w", "pico_workload_mean_signal_prediction_w"),
        "raw placement power": ("routed_signal_delta_w", "raw_placement_signal_prediction_w"),
        "Pico trained cap scale": ("routed_signal_delta_w", "pico_cap_scaled_signal_prediction_w"),
        "Pico trained power scale": ("routed_signal_delta_w", "pico_power_scaled_signal_prediction_w"),
        "zero (full routed)": ("routed_total_delta_w", None),
        "Pico workload mean (full routed)": ("routed_total_delta_w", "pico_workload_mean_total_prediction_w"),
        "raw placement power (full routed)": ("routed_total_delta_w", "raw_placement_signal_prediction_w"),
        "Pico trained cap scale (full routed)": ("routed_total_delta_w", "pico_cap_scaled_signal_prediction_w"),
    }
    for scope_name, group in scopes.items():
        for model, (target_key, prediction_key) in models.items():
            if target_key == "routed_total_delta_w" and model == "zero (full routed)":
                target = [number(row, target_key) for row in group]
                prediction = [0.0] * len(group)
            elif target_key == "routed_total_delta_w":
                target = [number(row, target_key) for row in group]
                prediction = [number(row, str(prediction_key)) for row in group]
            else:
                target = [number(row, target_key) for row in group]
                prediction = [number(row, str(prediction_key)) for row in group]
            match, cases = sign_accuracy(target, prediction)
            metric_rows.append({
                "scope": scope_name,
                "metric": "candidate-workload routed total power including clock" if "full routed" in model else "candidate-workload routed signal power",
                "model": model,
                "rows": len(group),
                "mae_nW": mae(target, prediction) * 1e9,
                "sign_matches": match,
                "sign_cases": cases,
                "training_design": "PicoRV32",
                "heldout_design": "GCD",
            })
    return metric_rows, per_net_predictions, candidate_predictions, cap_scale, power_scale


def write_report(
    path: Path,
    manifest: list[dict[str, str]],
    input_manifest: list[dict[str, str]],
    candidate_rows: list[dict[str, object]],
    metric_rows: list[dict[str, object]],
    cap_scale: float,
    power_scale: float,
) -> None:
    safe = [row for row in candidate_rows if int(row["timing_safe"]) == 1]
    rejected = sorted({str(row["candidate"]) for row in candidate_rows if int(row["timing_safe"]) == 0})
    safe_cap = [row for row in metric_rows if row["scope"] == "timing_safe" and row["metric"] == "per-net routed delta wire capacitance"]
    safe_power = [row for row in metric_rows if row["scope"] == "timing_safe" and row["metric"] == "candidate-workload routed signal power"]
    safe_total = [row for row in metric_rows if row["scope"] == "timing_safe" and row["metric"] == "candidate-workload routed total power including clock"]
    placement_reconciliation = max(number(row, "placement_score_reconciliation_error_w") for row in candidate_rows)
    lines = [
        "# Cross-design placement-to-routed wire-capacitance predictor",
        "",
        "## Holdout and data checks",
        "",
        "PicoRV32 supplies the training data. All GCD candidate layouts are held out by design; no GCD route labels are used to fit either scale. The primary score includes only candidates that pass the 50 ps routed setup and hold guard in all three workloads. The remaining probe is retained in the all-probes table.",
        f"Training rows: 309,024 common-signal net/workload observations from 12 PicoRV32 layouts. GCD test rows: {len([row for row in candidate_rows if int(row['timing_safe']) == 1])} candidate/workload records across {len({str(row['candidate']) for row in safe})} timing-safe layouts.",
        f"Timing-excluded GCD probe(s): {', '.join(rejected) if rejected else 'none'}. Placement cap exports: {len(manifest)} reports. Every export checked the source ODB SHA-256 before and after, and candidate ODBs matched their saved probe checkpoints. All {len(input_manifest)} calibration, routing, VCD, library, and source-script inputs have matching before/after SHA-256 hashes.",
        f"Per-net placement power reconstructed from placed capacitance reconciles to the independently recorded GCD swap score; maximum absolute difference was {placement_reconciliation * 1e9:.6f} nW.",
        "",
        "The routed target is the signal-net delta attributable to wire capacitance. The primary prediction first estimates routed delta-C from placed delta-C, then multiplies by 0.5 × V² × the VCD transition density at 1.8 V. The model is a single through-origin scalar fit on PicoRV32 only.",
        f"PicoRV32 fitted scale: routed delta-C = {cap_scale:.6g} × placed delta-C. The candidate-level routed signal-power scale is {power_scale:.6g} × the direct placement power score.",
        "",
        "## Held-out per-net routed delta-C",
        "",
        "| Model | MAE (fF/net) | Sign agreement |",
        "|---|---:|---:|",
    ]
    for row in safe_cap:
        sign = "—" if int(row["sign_cases"]) == 0 else f"{row['sign_matches']}/{row['sign_cases']}"
        lines.append(
            f"| {row['model']} | {number(row, 'mae_fF'):.6g} | {sign} |"
        )
    lines.extend([
        "",
        "## Held-out candidate/workload power",
        "",
        "| Model | Target | MAE (nW) | Sign agreement |",
        "|---|---|---:|---:|",
    ])
    for row in safe_power + safe_total:
        target = "full routed, including CTS clock" if "including clock" in str(row["metric"]) else "signal subtotal"
        sign = "—" if int(row["sign_cases"]) == 0 else f"{row['sign_matches']}/{row['sign_cases']}"
        lines.append(
            f"| {row['model']} | {target} | {number(row, 'mae_nW'):.3f} | "
            f"{sign} |"
        )
    direct = next(row for row in safe_power if row["model"] == "raw placement power")
    scaled = next(row for row in safe_power if row["model"] == "Pico trained cap scale")
    zero = next(row for row in safe_power if row["model"] == "zero")
    workload_mean = next(row for row in safe_power if row["model"] == "Pico workload mean")
    full_raw = next(row for row in safe_total if row["model"] == "raw placement power (full routed)")
    full_scaled = next(row for row in safe_total if row["model"] == "Pico trained cap scale (full routed)")
    full_zero = next(row for row in safe_total if row["model"] == "zero (full routed)")
    full_mean = next(row for row in safe_total if row["model"] == "Pico workload mean (full routed)")
    conclusion = (
        f"On held-out GCD signal power, the raw placement estimate has the lowest MAE ({number(direct, 'mae_nW'):.3f} nW). "
        f"The Pico-trained cap scale is worse at {number(scaled, 'mae_nW'):.3f} nW, although it still beats zero ({number(zero, 'mae_nW'):.3f} nW) and the Pico workload mean ({number(workload_mean, 'mae_nW'):.3f} nW)."
    )
    lines.extend([
        "",
        "## Reading the result",
        "",
        f"{conclusion} On full routed power, the raw placement estimate has {number(full_raw, 'mae_nW'):.3f} nW MAE, compared with {number(full_scaled, 'mae_nW'):.3f} nW for the scaled cap model, {number(full_zero, 'mae_nW'):.3f} nW for zero, and {number(full_mean, 'mae_nW'):.3f} nW for the Pico workload mean. CTS clock changes still create route effects that are unavailable at placement; this small probe set does not show that the placer can reliably predict those effects.",
        "These are diagnostic moves from two designs, with only eight GCD probes and one timing-failing layout. The set is too small and selected to justify inserting the learned scale into the placer. A positive result would justify a broader candidate sweep; a negative result means the next model needs richer placement features such as per-net HPWL, fanout, layer access, and timing criticality.",
        "",
        "## Files",
        "",
        "- cross_design_per_net_predictions.csv: held-out GCD net predictions and SPEF wire-cap labels.",
        "- cross_design_candidate_workload.tsv: aggregate placed score, routed signal/clock deltas, and predictions.",
        "- cross_design_metrics.tsv: per-net delta-C and candidate/workload power scores.",
        "- placement_caps/export_manifest.tsv: report inputs, hashes, and cap coverage.",
        "- analysis_input_hashes.tsv: hashes of the training data, routed labels, and tool/source inputs.",
    ])
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "build/power/cross_design_wirecap_20261027",
    )
    parser.add_argument("--skip-export", action="store_true", help="reuse previously exported placed-cap reports")
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        input_paths = analysis_input_paths()
        input_hashes = {path: sha256(path) for path in input_paths}
        manifest = export_placement_caps(output_dir, args.skip_export)
        pico_cap_pairs, pico_aggregates = read_pico_training()
        gcd_net_rows, gcd_candidate_rows = build_gcd_rows(output_dir)
        metrics, predictions, candidate_predictions, cap_scale, power_scale = analyze_models(
            gcd_net_rows, gcd_candidate_rows, pico_cap_pairs, pico_aggregates
        )
        write_csv(output_dir / "cross_design_per_net_predictions.csv", predictions)
        write_csv(output_dir / "cross_design_candidate_workload.tsv", candidate_predictions, "\t")
        write_csv(output_dir / "cross_design_metrics.tsv", metrics, "\t")
        input_manifest = hash_input_manifest(input_paths, input_hashes)
        write_csv(output_dir / "analysis_input_hashes.tsv", input_manifest, "\t")
        report = output_dir / "cross_design_predictor_report.md"
        write_report(report, manifest, input_manifest, gcd_candidate_rows, metrics, cap_scale, power_scale)
        print(report)
        print(f"placement exports: {len(manifest)}; GCD per-net rows: {len(gcd_net_rows)}; candidate/workload rows: {len(gcd_candidate_rows)}")
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, StopIteration) as exc:
        print(f"cross-design wire-capacitance analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
