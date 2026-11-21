#!/usr/bin/env python3
"""Measure VCD power at CTS and global-route checkpoints; compare with SPEF targets."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from analyze_unified_place_spef_calibration import (
    OLD_CANDIDATES,
    ROOT,
    WORKLOADS,
    check_manifest,
    fit_scale,
    mae,
    pearson,
    read_csv,
    write_csv,
)


VOLTAGE = 1.8
SIGNAL_NET_COUNT = 8584
BASELINE_RESULT = ROOT / "build/orfs/results/sky130hd/picorv32_baseline/baseline"
RESULTS_ROOT = ROOT / "build/orfs/results/sky130hd/picorv32_baseline"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
TCL = ROOT / "scripts/report_stage_power.tcl"
PAIR_ROOT = ROOT / "build/power/vcd_swap_audit_20261027_locsearch01/pair_calibration"
PAIR_VARIANTS = {
    "swap44": "vcdswap_20261027_locsearch01_swap44",
    "swap52": "vcdswap_20261027_locsearch01_swap52",
}
STAGES = {
    "cts_placement": ("4_cts.odb", "4_cts.sdc", "placement"),
    "global_route": ("5_1_grt.odb", "5_1_grt.sdc", "global_routing"),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_vcd_sha(path: Path) -> str:
    data = path.read_bytes()
    data = re.sub(rb"\$date.*?\$end\s*", b"", data, count=1, flags=re.S)
    return hashlib.sha256(data).hexdigest()


def as_float(value: object) -> float:
    return float(value or 0.0)


def candidate_sources() -> dict[str, dict[str, Path | str]]:
    result: dict[str, dict[str, Path | str]] = {}
    for label, calibration_dir in OLD_CANDIDATES.items():
        manifest = check_manifest(calibration_dir / "manifest.tsv")
        variant = manifest["routed_flow_variant"]
        activity_dir = ROOT / "build/activity" / calibration_dir.name
        result[label] = {"variant": variant, "activity_dir": activity_dir}
    for label, variant in PAIR_VARIANTS.items():
        activity_dir = ROOT / "build/activity" / variant
        result[label] = {"variant": variant, "activity_dir": activity_dir}
    return result


def stage_paths(variant: str, stage: str) -> tuple[Path, Path]:
    odb_name, sdc_name, _ = STAGES[stage]
    directory = RESULTS_ROOT / variant
    return directory / odb_name, directory / sdc_name


def parse_caps(path: Path) -> tuple[dict[str, dict[str, object]], float]:
    header: list[str] | None = None
    by_net: dict[str, dict[str, object]] = {}
    formula_power = 0.0
    with path.open() as source:
        for line in source:
            if not line.startswith("#STAGE_DRIVER_CAP\t"):
                continue
            parts = line.rstrip("\n").split("\t")
            if parts[1] == "pin":
                header = parts[1:]
                continue
            if header is None or len(parts) != len(header) + 1:
                raise ValueError(f"malformed driver-cap row in {path}")
            row = dict(zip(header, parts[1:]))
            net = str(row["net"])
            if net in by_net:
                raise ValueError(f"multiple driver rows found for net {net} in {path}")
            cap_f = as_float(row["total_cap"])
            density = as_float(row["density_hz"])
            power_w = 0.5 * VOLTAGE**2 * cap_f * density
            row["power_w"] = power_w
            by_net[net] = row
            formula_power += power_w
    if not by_net:
        raise ValueError(f"no driver-cap rows found in {path}")
    return by_net, formula_power


def parse_log(log: str, stage: str, workload: str, cap_path: Path, log_path: Path) -> dict[str, object]:
    match = re.search(
        r"STAGE_METRICS stage=\S+ workload=\S+ switching_w=([0-9.eE+-]+) "
        r"setup_ns=([0-9.eE+-]+) hold_ns=([0-9.eE+-]+)",
        log,
    )
    if not match:
        raise ValueError(f"power/timing metrics missing from {log_path}")
    annotated_match = re.search(r"Annotated (\d+) pin activities", log)
    if not annotated_match:
        raise ValueError(f"VCD annotation count missing from {log_path}")
    unannotated_match = re.search(r"^unannotated\s+(\d+)\s*$", log, re.M)
    caps, formula_power = parse_caps(cap_path)
    sta_power = float(match.group(1))
    ppm = (formula_power - sta_power) / max(abs(sta_power), 1e-30) * 1e6
    return {
        "stage": stage,
        "workload": workload,
        "switching_w": sta_power,
        "setup_ns": float(match.group(2)),
        "hold_ns": float(match.group(3)),
        "annotated_pins": int(annotated_match.group(1)),
        "unannotated_pins": int(unannotated_match.group(1)) if unannotated_match else "",
        "formula_power_w": formula_power,
        "formula_reconciliation_ppm": ppm,
        "caps": caps,
        "log_path": str(log_path),
        "cap_path": str(cap_path),
    }


def run_stage(
    stage: str,
    workload: str,
    variant: str,
    vcd_path: Path,
    output_base: Path,
    mode: str,
    label: str,
) -> dict[str, object]:
    odb_path, sdc_path = stage_paths(variant, stage)
    if not OPENROAD.is_file() or not odb_path.is_file() or not sdc_path.is_file() or not vcd_path.is_file():
        raise ValueError(f"missing OpenROAD input for {label}/{stage}/{workload}")
    cap_path = output_base.with_suffix(".caps.tsv")
    log_path = output_base.with_suffix(".log")
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({
        "STAGE": stage,
        "PARASITIC_MODE": mode,
        "STAGE_ODB": str(odb_path),
        "STAGE_SDC": str(sdc_path),
        "ACTIVITY_VCD": str(vcd_path),
        "WORKLOAD": workload,
        "CAP_REPORT": str(cap_path),
    })
    process = subprocess.run(
        [str(OPENROAD), "-no_init", "-exit", str(TCL)],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(process.stdout)
    if process.returncode != 0:
        raise ValueError(f"OpenROAD failed for {label}/{stage}/{workload}; see {log_path}")
    return parse_log(process.stdout, stage, workload, cap_path, log_path)


def load_or_run_stage(
    reuse_raw: bool,
    stage: str,
    workload: str,
    variant: str,
    vcd_path: Path,
    output_base: Path,
    mode: str,
    label: str,
) -> dict[str, object]:
    if not reuse_raw:
        return run_stage(stage, workload, variant, vcd_path, output_base, mode, label)
    cap_path = output_base.with_suffix(".caps.tsv")
    log_path = output_base.with_suffix(".log")
    if not cap_path.is_file() or not log_path.is_file():
        raise ValueError(f"raw output missing for --reuse-raw: {label}/{stage}/{workload}")
    return parse_log(log_path.read_text(), stage, workload, cap_path, log_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "build/power/place_spef_cts_grt_20261027_locsearch01",
    )
    parser.add_argument("--reuse-raw", action="store_true", help="recompute reports from existing OpenROAD logs/cap tables")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    try:
        if output.exists() and any(output.iterdir()) and not args.reuse_raw:
            raise ValueError(f"output directory already contains files: {output}")
        sources = candidate_sources()
        unified_path = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01/unified_per_net_features.csv"
        summary_path = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01/candidate_workload_attribution.tsv"
        unified_rows = read_csv(unified_path)
        summary_rows = read_csv(summary_path, "\t")
        labels = {row["candidate"] for row in unified_rows}
        if labels != set(sources):
            raise ValueError(f"candidate mismatch: unified={sorted(labels)} stage sources={sorted(sources)}")
        if len(unified_rows) != len(labels) * len(WORKLOADS) * SIGNAL_NET_COUNT:
            raise ValueError(f"expected full common-net coverage, found {len(unified_rows)} rows")

        # Recheck pair-swap input snapshots and all old calibration manifests.
        if (PAIR_ROOT / "input_hashes_before.tsv").read_bytes() != (PAIR_ROOT / "input_hashes_after.tsv").read_bytes():
            raise ValueError("pair-swap calibration input hashes changed between its before/after snapshots")
        baseline_vcd: dict[tuple[str, str], Path] = {}
        candidate_vcds: dict[tuple[str, str], Path] = {}
        baseline_classes: dict[tuple[str, str], str] = {}
        for candidate, source in sources.items():
            activity_dir = Path(source["activity_dir"])
            for workload in WORKLOADS:
                baseline = activity_dir / "baseline" / f"{workload}.vcd"
                candidate_activity = activity_dir / "candidate" / f"{workload}.vcd"
                if not baseline.is_file() or not candidate_activity.is_file():
                    raise ValueError(f"missing matched VCD for {candidate}/{workload}")
                baseline_vcd[(candidate, workload)] = baseline
                candidate_vcds[(candidate, workload)] = candidate_activity
                baseline_classes[(candidate, workload)] = normalized_vcd_sha(baseline)

        # Verify every checkpoint and matching SDC exists before starting.
        for candidate, source in sources.items():
            for stage in STAGES:
                odb, sdc = stage_paths(str(source["variant"]), stage)
                if not odb.is_file() or not sdc.is_file():
                    raise ValueError(f"missing {stage} checkpoint for {candidate}: {odb} / {sdc}")
        for stage in STAGES:
            odb, sdc = stage_paths("baseline", stage)
            if not odb.is_file() or not sdc.is_file():
                raise ValueError(f"missing baseline {stage} checkpoint")

        output.mkdir(parents=True, exist_ok=True)
        raw_dir = output / "raw"
        baseline_representatives: dict[tuple[str, str], Path] = {}
        for candidate in sorted(sources):
            for workload in WORKLOADS:
                key = (workload, baseline_classes[(candidate, workload)])
                baseline_representatives.setdefault(key, baseline_vcd[(candidate, workload)])

        # Cache baseline stage results by workload and normalized VCD contents.
        baseline_results: dict[tuple[str, str, str], dict[str, object]] = {}
        baseline_tasks = []
        for (workload, class_hash), vcd in sorted(baseline_representatives.items()):
            class_short = class_hash[:12]
            for stage, (_, _, mode) in STAGES.items():
                key = (stage, workload, class_hash)
                baseline_tasks.append((key, vcd, stage, workload, mode, class_short))

        total_tasks = len(baseline_tasks) + len(sources) * len(WORKLOADS) * len(STAGES)
        source_paths = {BASELINE_RESULT / "4_cts.odb", BASELINE_RESULT / "4_cts.sdc",
                        BASELINE_RESULT / "5_1_grt.odb", BASELINE_RESULT / "5_1_grt.sdc",
                        unified_path, summary_path,
                        PAIR_ROOT / "input_hashes_before.tsv", PAIR_ROOT / "input_hashes_after.tsv"}
        source_paths.update(path / "manifest.tsv" for path in OLD_CANDIDATES.values())
        for source in sources.values():
            for stage in STAGES:
                source_paths.update(stage_paths(str(source["variant"]), stage))
            activity_dir = Path(source["activity_dir"])
            for workload in WORKLOADS:
                source_paths.add(activity_dir / "baseline" / f"{workload}.vcd")
                source_paths.add(activity_dir / "candidate" / f"{workload}.vcd")
        source_paths = {path.resolve() for path in source_paths}
        before = {path: sha256(path) for path in sorted(source_paths)}
        done = 0
        for key, vcd, stage, workload, mode, class_short in baseline_tasks:
            done += 1
            print(f"[{done}/{total_tasks}] baseline {class_short} {stage} {workload}", flush=True)
            baseline_results[key] = load_or_run_stage(
                args.reuse_raw,
                stage,
                workload,
                "baseline",
                vcd,
                raw_dir / stage / "baseline" / class_short / workload,
                mode,
                f"baseline:{class_short}",
            )

        targets: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
        routed_totals: dict[tuple[str, str], dict[str, float]] = {}
        for row in unified_rows:
            key = (row["candidate"], row["workload"])
            targets[key][row["net"]] = float(row["routed_power_delta_w"])
        for row in summary_rows:
            routed_totals[(row["candidate"], row["workload"])] = {
                "signal": float(row["routed_signal_delta_w"]),
                "total": float(row["routed_total_per_net_delta_w"]),
            }

        attribution = []
        stage_prediction_rows: list[dict[str, object]] = []
        pernet_metric_inputs: dict[str, dict[str, list[object]]] = {
            stage: {"x": [], "y": [], "candidate": []} for stage in STAGES
        }
        net_output = output / "stage_signal_per_net_deltas.tsv"
        with net_output.open("w", newline="") as net_stream:
            net_writer = csv.writer(net_stream, delimiter="\t")
            net_writer.writerow([
                "stage", "candidate", "workload", "net", "baseline_cap_f", "candidate_cap_f",
                "baseline_density_hz", "candidate_density_hz", "stage_signal_delta_w",
                "routed_signal_delta_w", "stage_minus_routed_error_w",
            ])
            for candidate in sorted(sources):
                variant = str(sources[candidate]["variant"])
                for workload in WORKLOADS:
                    common = targets[(candidate, workload)]
                    baseline_class = baseline_classes[(candidate, workload)]
                    for stage, (_, _, mode) in STAGES.items():
                        done += 1
                        print(f"[{done}/{total_tasks}] {candidate} {stage} {workload}", flush=True)
                        result = load_or_run_stage(
                            args.reuse_raw,
                            stage,
                            workload,
                            variant,
                            candidate_vcds[(candidate, workload)],
                            raw_dir / stage / "candidate" / candidate / workload,
                            mode,
                            candidate,
                        )
                        base_key = (stage, workload, baseline_class)
                        if base_key not in baseline_results:
                            raise ValueError(f"missing cached baseline estimate for {candidate}/{stage}/{workload}")
                        baseline = baseline_results[base_key]
                        base_caps = baseline["caps"]
                        candidate_caps = result["caps"]
                        missing_baseline = sum(net not in base_caps for net in common)
                        missing_candidate = sum(net not in candidate_caps for net in common)
                        if missing_baseline or missing_candidate:
                            raise ValueError(
                                f"common signal coverage failure for {candidate}/{stage}/{workload}: "
                                f"baseline missing={missing_baseline}, candidate missing={missing_candidate}"
                            )
                        signal_delta = 0.0
                        for net, actual in common.items():
                            baseline_row = base_caps[net]
                            candidate_row = candidate_caps[net]
                            estimate = as_float(candidate_row["power_w"]) - as_float(baseline_row["power_w"])
                            signal_delta += estimate
                            pernet_metric_inputs[stage]["x"].append(estimate)
                            pernet_metric_inputs[stage]["y"].append(actual)
                            pernet_metric_inputs[stage]["candidate"].append(candidate)
                            net_writer.writerow([
                                stage, candidate, workload, net,
                                baseline_row["total_cap"], candidate_row["total_cap"],
                                baseline_row["density_hz"], candidate_row["density_hz"],
                                estimate, actual, estimate - actual,
                            ])
                        total_delta = as_float(result["switching_w"]) - as_float(baseline["switching_w"])
                        actual_totals = routed_totals[(candidate, workload)]
                        setup_delta = as_float(result["setup_ns"]) - as_float(baseline["setup_ns"])
                        hold_delta = as_float(result["hold_ns"]) - as_float(baseline["hold_ns"])
                        attribution.append({
                            "stage": stage,
                            "candidate": candidate,
                            "workload": workload,
                            "baseline_vcd_class": baseline_class[:12],
                            "baseline_switching_w": baseline["switching_w"],
                            "candidate_switching_w": result["switching_w"],
                            "stage_total_delta_w": total_delta,
                            "routed_signal_target_w": actual_totals["signal"],
                            "routed_total_target_w": actual_totals["total"],
                            "stage_signal_delta_w": signal_delta,
                            "setup_delta_ns": setup_delta,
                            "hold_delta_ns": hold_delta,
                            "timing_guard_50ps_pass": setup_delta >= -0.05 and hold_delta >= -0.05,
                            "baseline_annotated_pins": baseline["annotated_pins"],
                            "candidate_annotated_pins": result["annotated_pins"],
                            "baseline_unannotated_pins": baseline["unannotated_pins"],
                            "candidate_unannotated_pins": result["unannotated_pins"],
                            "baseline_formula_reconciliation_ppm": baseline["formula_reconciliation_ppm"],
                            "candidate_formula_reconciliation_ppm": result["formula_reconciliation_ppm"],
                            "matched_signal_nets": len(common),
                            "baseline_covered_signal_nets": len(common) - missing_baseline,
                            "candidate_covered_signal_nets": len(common) - missing_candidate,
                        })
                        stage_prediction_rows.append({
                            "candidate": candidate,
                            "workload": workload,
                            "stage": stage,
                            "signal_feature": signal_delta,
                            "total_feature": total_delta,
                            "routed_signal_target": actual_totals["signal"],
                            "routed_total_target": actual_totals["total"],
                        })

        # Hash every source before and after the OpenROAD reads to prove no checkpoint/VCD changed.
        after = {path: sha256(path) for path in sorted(source_paths)}
        integrity_rows = [{"path": str(path), "sha256_before": before[path], "sha256_after": after[path],
                           "unchanged": before[path] == after[path]} for path in sorted(source_paths)]
        write_csv(output / "integrity.tsv", integrity_rows, "\t")
        if any(before[path] != after[path] for path in source_paths):
            raise ValueError("one or more stage checkpoint or activity input hashes changed")

        # Candidate/workload aggregate LOCO comparisons against zero and training means.
        aggregate_metrics = []
        aggregate_predictions = []
        fold_rows = []
        labels = sorted(sources)
        for stage in STAGES:
            stage_rows = [row for row in stage_prediction_rows if row["stage"] == stage]
            candidates_by_row = [str(row["candidate"]) for row in stage_rows]
            workloads_by_row = [str(row["workload"]) for row in stage_rows]
            for measure in ("signal", "total"):
                feature_name = f"{stage}_{measure}"
                target_kind = measure
                xs = [float(row[f"{measure}_feature"]) for row in stage_rows]
                ys = [float(row[f"routed_{measure}_target"]) for row in stage_rows]
                model_pred = [0.0] * len(ys)
                global_mean_pred = [0.0] * len(ys)
                workload_mean_pred = [0.0] * len(ys)
                scales = {}
                for held in labels:
                    train = [i for i, label in enumerate(candidates_by_row) if label != held]
                    test = [i for i, label in enumerate(candidates_by_row) if label == held]
                    scale = fit_scale([xs[i] for i in train], [ys[i] for i in train])
                    scales[held] = scale
                    global_mean = sum(ys[i] for i in train) / len(train)
                    for i in test:
                        same_workload = [j for j in train if workloads_by_row[j] == workloads_by_row[i]]
                        model_pred[i] = scale * xs[i]
                        global_mean_pred[i] = global_mean
                        workload_mean_pred[i] = sum(ys[j] for j in same_workload) / len(same_workload)
                direction_cases = sum(abs(p) > 1e-15 and abs(y) > 1e-15 for p, y in zip(model_pred, ys))
                target_name = "routed_signal_delta_w" if target_kind == "signal" else "routed_total_per_net_delta_w"
                aggregate_metrics.append({
                    "feature": feature_name,
                    "target": target_name,
                    "rows": len(ys),
                    "zero_mae_w": mae(ys, [0.0] * len(ys)),
                    "global_fold_mean_mae_w": mae(ys, global_mean_pred),
                    "workload_fold_mean_mae_w": mae(ys, workload_mean_pred),
                    "direct_stage_mae_w": mae(ys, xs),
                    "loco_scaled_mae_w": mae(ys, model_pred),
                    "loco_direction_matches": sum(p * y > 0 for p, y in zip(model_pred, ys) if abs(p) > 1e-15 and abs(y) > 1e-15),
                    "loco_direction_cases": direction_cases,
                    "pearson_r": pearson(xs, ys) if pearson(xs, ys) is not None else "",
                    "folds_beating_workload_mean": "",
                })
                for index, row in enumerate(stage_rows):
                    aggregate_predictions.append({
                        "feature": feature_name,
                        "target": target_name,
                        "candidate": row["candidate"],
                        "workload": row["workload"],
                        "actual_w": ys[index],
                        "direct_stage_prediction_w": xs[index],
                        "loco_scale": scales[str(row["candidate"])],
                        "loco_prediction_w": model_pred[index],
                        "global_fold_mean_prediction_w": global_mean_pred[index],
                        "workload_fold_mean_prediction_w": workload_mean_pred[index],
                    })
                for candidate in labels:
                    test = [i for i, label in enumerate(candidates_by_row) if label == candidate]
                    fold_error = sum(abs(ys[i] - model_pred[i]) for i in test) / len(test)
                    workload_error = sum(abs(ys[i] - workload_mean_pred[i]) for i in test) / len(test)
                    fold_rows.append({
                        "feature": feature_name,
                        "target": target_name,
                        "candidate": candidate,
                        "workloads": len(test),
                        "loco_mae_w": fold_error,
                        "workload_mean_mae_w": workload_error,
                        "beats_workload_mean": fold_error < workload_error,
                    })
        fold_counts = defaultdict(int)
        for row in fold_rows:
            fold_counts[(row["feature"], row["target"])] += int(bool(row["beats_workload_mean"]))
        for row in aggregate_metrics:
            row["folds_beating_workload_mean"] = f"{fold_counts[(row['feature'], row['target'])]}/{len(labels)}"

        pernet_metrics = []
        for stage, vectors in pernet_metric_inputs.items():
            xs = [float(value) for value in vectors["x"]]
            ys = [float(value) for value in vectors["y"]]
            candidate_labels = [str(value) for value in vectors["candidate"]]
            pred = [0.0] * len(ys)
            for held in sorted(set(candidate_labels)):
                train = [i for i, candidate in enumerate(candidate_labels) if candidate != held]
                test = [i for i, candidate in enumerate(candidate_labels) if candidate == held]
                scale = fit_scale([xs[i] for i in train], [ys[i] for i in train])
                for i in test:
                    pred[i] = scale * xs[i]
            pernet_metrics.append({
                "stage": stage,
                "rows": len(ys),
                "zero_mae_w": mae(ys, [0.0] * len(ys)),
                "direct_stage_mae_w": mae(ys, xs),
                "loco_scaled_mae_w": mae(ys, pred),
                "pearson_r": pearson(xs, ys) if pearson(xs, ys) is not None else "",
            })

        write_csv(output / "candidate_workload_stage_attribution.tsv", attribution, "\t")
        write_csv(output / "candidate_stage_loco_metrics.tsv", aggregate_metrics, "\t")
        write_csv(output / "candidate_stage_loco_predictions.tsv", aggregate_predictions, "\t")
        write_csv(output / "candidate_stage_fold_summary.tsv", fold_rows, "\t")
        write_csv(output / "per_net_stage_loco_metrics.tsv", pernet_metrics, "\t")

        max_ppm = max(
            abs(float(row[key]))
            for row in attribution
            for key in ("baseline_formula_reconciliation_ppm", "candidate_formula_reconciliation_ppm")
        )
        min_annotation = min(int(row["candidate_annotated_pins"]) for row in attribution)
        timing_pass_counts = {
            stage: sum(bool(row["timing_guard_50ps_pass"]) for row in attribution if row["stage"] == stage)
            for stage in STAGES
        }
        lines = [
            "# CTS and global-route VCD power calibration",
            "",
            "## Checkpoint coverage and integrity",
            "",
            f"Measured the existing CTS-placement and global-route checkpoints for {len(labels)} candidate layouts and three workloads. The VCD-aware activity/capacitance calculation covered all {SIGNAL_NET_COUNT} common signal nets per candidate/workload. VCD baselines were grouped only when their waveforms matched after removing the simulation date header.",
            f"OpenSTA's reported switching power reconciled with the independently summed 0.5 × C × V² × transition-rate estimate to a maximum absolute error of {max_ppm:.3f} ppm. Minimum candidate pin-activity annotation count was {min_annotation}. Input ODB, SDC, and VCD hashes were recorded before/after and checked unchanged.",
            f"All-workload 50 ps setup/hold guard passes: CTS-placement {timing_pass_counts['cts_placement']}/{len(labels)*len(WORKLOADS)}; global route {timing_pass_counts['global_route']}/{len(labels)*len(WORKLOADS)}.",
            "",
            "## Candidate/workload held-out comparison",
            "",
            "MAE is over 36 candidate/workload samples. The workload-conditioned fold mean uses the other 11 candidates' result for the same workload. Direct stage delta is candidate minus baseline at that checkpoint; the LOCO scale is fitted through zero using only the other candidates.",
            "",
            "| Stage feature | Routed target | Zero MAE | Workload mean MAE | Direct stage MAE | LOCO scaled MAE | Folds beating mean |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for row in aggregate_metrics:
            target = "signal subtotal" if row["target"] == "routed_signal_delta_w" else "full total incl. clock"
            lines.append(
                f"| {row['feature']} | {target} | {float(row['zero_mae_w'])*1e6:.3f} µW | "
                f"{float(row['workload_fold_mean_mae_w'])*1e6:.3f} µW | "
                f"{float(row['direct_stage_mae_w'])*1e6:.3f} µW | "
                f"{float(row['loco_scaled_mae_w'])*1e6:.3f} µW | {row['folds_beating_workload_mean']} |"
            )
        lines.extend([
            "",
            "## Per-net routed signal comparison",
            "",
            "The feature is the candidate-minus-baseline VCD-weighted switching power for each common signal net, using that checkpoint's estimated pin plus wire capacitance. The held-out scale is fit by candidate, keeping each candidate and all three workloads together.",
            "",
            "| Stage | Rows | Zero MAE | Direct stage MAE | LOCO scaled MAE | Pearson r |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for row in pernet_metrics:
            lines.append(
                f"| {row['stage']} | {row['rows']} | {float(row['zero_mae_w'])*1e9:.4f} nW/net | "
                f"{float(row['direct_stage_mae_w'])*1e9:.4f} nW/net | "
                f"{float(row['loco_scaled_mae_w'])*1e9:.4f} nW/net | {float(row['pearson_r']):+.4f} |"
            )
        lines.extend([
            "",
            "## Outputs",
            "",
            "- `candidate_workload_stage_attribution.tsv`: CTS/global-route stage deltas, timing, annotation coverage, and actual routed targets.",
            "- `candidate_stage_loco_metrics.tsv`, `candidate_stage_loco_predictions.tsv`, `candidate_stage_fold_summary.tsv`: candidate-held-out aggregate comparisons.",
            "- `per_net_stage_loco_metrics.tsv`: held-out per-net comparison against final routed SPEF signal deltas.",
            "- `stage_signal_per_net_deltas.tsv`: per-net estimated power deltas and routed targets for both stages.",
            "- `raw/`: OpenROAD logs and per-driver capacitance/activity tables for every measured checkpoint.",
            "- `integrity.tsv`: source checkpoint and VCD hashes before/after analysis.",
        ])
        (output / "stage_power_calibration_report.md").write_text("\n".join(lines) + "\n")
        print(f"Completed {total_tasks} OpenROAD stage analyses; max formula reconciliation {max_ppm:.3f} ppm")
        print(output / "stage_power_calibration_report.md")
        return 0
    except (OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        print(f"CTS/global-route power calibration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
