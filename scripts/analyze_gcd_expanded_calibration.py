#!/usr/bin/env python3
"""Evaluate an expanded GCD placement-to-routed power calibration batch."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import statistics
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE**2
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
PLACE_REPORT = ROOT / "scripts/gcd_report_place_power.tcl"
GCD_BASELINE = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
ACTIVITY_ROOT = ROOT / "build/activity/gcd_baseline20ns/placement"
PICO_ANALYSIS = ROOT / "build/power/place_spef_unified_analysis_20261027_locsearch01"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def write_table(path: Path, rows: list[dict[str, object]], delimiter: str = "\t") -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=list(dict.fromkeys(k for row in rows for k in row)),
            delimiter=delimiter, lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def number(row: dict[str, str] | dict[str, object], key: str) -> float:
    return float(row.get(key, 0) or 0.0)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fit_scale(pairs: list[tuple[float, float]]) -> float:
    denominator = sum(x * x for x, _ in pairs)
    return sum(x * y for x, y in pairs) / denominator if denominator else 0.0


def mae(actual: list[float], predicted: list[float]) -> float:
    if not actual or len(actual) != len(predicted):
        raise ValueError("MAE inputs must be nonempty and have equal lengths")
    return statistics.mean(abs(a - p) for a, p in zip(actual, predicted))


def sign_score(actual: list[float], predicted: list[float]) -> tuple[int, int]:
    cases = [(a, p) for a, p in zip(actual, predicted) if abs(a) > 1e-18 and abs(p) > 1e-18]
    return sum((a > 0) == (p > 0) for a, p in cases), len(cases)


def read_place_caps(path: Path) -> dict[str, dict[str, str | float]]:
    caps: dict[str, dict[str, str | float]] = {}
    with path.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 6:
                raise ValueError(f"malformed placement cap row in {path}: {line!r}")
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            if pin == "pin":
                continue
            if pin in caps:
                raise ValueError(f"duplicate placement driver pin {pin} in {path}")
            caps[pin] = {
                "net": net,
                "pin_cap_f": float(pin_cap),
                "wire_cap_f": float(wire_cap),
                "total_cap_f": float(total_cap),
            }
    if not caps:
        raise ValueError(f"no placement driver capacitances found in {path}")
    return caps


def export_place_caps(output_dir: Path, candidates: list[str], skip_export: bool) -> list[dict[str, str]]:
    cap_root = output_dir / "placement_caps"
    manifest_path = cap_root / "export_manifest.tsv"
    if skip_export:
        manifest = read_tsv(manifest_path)
        for row in manifest:
            odb = Path(row["odb"])
            report = Path(row["cap_report"])
            if sha256(odb) != row["odb_sha256"] or sha256(report) != row["cap_sha256"]:
                raise ValueError(f"saved placement-cap export hash changed: {report}")
        return manifest

    targets = [("baseline", GCD_BASELINE / "3_5_place_dp.odb")]
    targets.extend(
        (candidate, output_dir / "candidate_odb" / f"{candidate}.odb")
        for candidate in candidates
    )
    baseline_sdc = GCD_BASELINE / "3_place.sdc"
    manifest: list[dict[str, str]] = []
    for label, odb in targets:
        if not odb.is_file():
            raise FileNotFoundError(f"missing placement ODB for {label}: {odb}")
        for workload in WORKLOADS:
            activity = ACTIVITY_ROOT / f"{workload}.vcd"
            report_dir = cap_root / label
            report_dir.mkdir(parents=True, exist_ok=True)
            cap_report = report_dir / f"{workload}.driver_caps.tsv"
            log_path = report_dir / f"{workload}.log"
            before = sha256(odb)
            env = os.environ.copy()
            env.update(
                PLACEMENT_ODB=str(odb), PLACEMENT_SDC=str(baseline_sdc),
                ACTIVITY_VCD=str(activity), ACTIVITY_TRACE="1", CAP_REPORT=str(cap_report),
            )
            with log_path.open("w") as log:
                result = subprocess.run(
                    [str(OPENROAD), "-no_init", "-exit", str(PLACE_REPORT)],
                    cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=False,
                )
            if result.returncode:
                raise RuntimeError(f"placement cap export failed for {label}/{workload}: {log_path}")
            if sha256(odb) != before:
                raise ValueError(f"read-only cap export changed source placement ODB: {odb}")
            cap_count = len(read_place_caps(cap_report))
            manifest.append({
                "candidate": label, "workload": workload, "odb": str(odb),
                "odb_sha256": before, "activity_vcd": str(activity),
                "activity_sha256": sha256(activity), "placement_sdc": str(baseline_sdc),
                "sdc_sha256": sha256(baseline_sdc), "cap_report": str(cap_report),
                "cap_sha256": sha256(cap_report), "driver_count": str(cap_count),
            })
    write_table(manifest_path, manifest)
    return manifest


def read_pico_models() -> tuple[float, float, dict[str, float], dict[str, float]]:
    features_path = PICO_ANALYSIS / "unified_per_net_features.csv"
    aggregate_path = PICO_ANALYSIS / "candidate_workload_attribution.tsv"
    features = read_csv(features_path)
    cap_pairs = [
        (number(row, "delta_place_wire_cap_f"), number(row, "routed_delta_wire_cap_f"))
        for row in features if row["routed_class"] == "common_signal"
    ]
    aggregates = read_tsv(aggregate_path)
    power_pairs = [
        (number(row, "placement_signal_prediction_w"), number(row, "routed_signal_delta_w"))
        for row in aggregates
    ]
    signal_mean = {
        workload: statistics.mean(
            number(row, "routed_signal_delta_w") for row in aggregates if row["workload"] == workload
        )
        for workload in WORKLOADS
    }
    total_mean = {
        workload: statistics.mean(
            number(row, "routed_total_per_net_delta_w") for row in aggregates if row["workload"] == workload
        )
        for workload in WORKLOADS
    }
    return fit_scale(cap_pairs), fit_scale(power_pairs), signal_mean, total_mean


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-selection", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--route-root", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--skip-export", action="store_true")
    args = parser.parse_args()

    try:
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        candidates = [row["pair_id"] for row in read_tsv(args.candidate_selection)]
        if not candidates:
            raise ValueError("candidate selection is empty")
        manifest = export_place_caps(output_dir, candidates, args.skip_export)
        cap_root = output_dir / "placement_caps"
        base_caps = {
            workload: read_place_caps(cap_root / "baseline" / f"{workload}.driver_caps.tsv")
            for workload in WORKLOADS
        }
        audit = {
            workload: {row["pair_id"]: row for row in read_tsv(args.audit_dir / f"audit_{workload}.tsv")}
            for workload in WORKLOADS
        }
        route_validation = read_tsv(args.output_dir / "route_analysis" / "route_validation.tsv")
        route_map = {(row["pair_id"], row["workload"]): row for row in route_validation}
        cap_scale, power_scale, pico_signal_mean, pico_total_mean = read_pico_models()

        per_net_rows: list[dict[str, object]] = []
        candidate_rows: list[dict[str, object]] = []
        for candidate in candidates:
            for workload in WORKLOADS:
                route_dir = args.route_root / f"gcd_cal_{candidate}_{args.tag}" / "net_attribution"
                route_items = read_csv(route_dir / f"{workload}.per_net_delta.csv")
                candidate_caps = read_place_caps(cap_root / candidate / f"{workload}.driver_caps.tsv")
                placed_power = 0.0
                routed_signal = 0.0
                for item in route_items:
                    if item["baseline_activity_origin"] == "clock" or item["net"].startswith("clknet_"):
                        continue
                    pin = item["baseline_driver_pin"]
                    if not pin:
                        raise ValueError(f"signal row has no baseline driver pin in {candidate}/{workload}")
                    if pin not in base_caps[workload] or pin not in candidate_caps:
                        raise ValueError(f"missing placement driver cap for {candidate}/{workload}/{pin}")
                    density = number(item, "baseline_density_hz")
                    place_delta_cap = (
                        float(candidate_caps[pin]["wire_cap_f"])
                        - float(base_caps[workload][pin]["wire_cap_f"])
                    )
                    place_power = POWER_FACTOR * density * place_delta_cap
                    route_delta_cap = number(item, "delta_wire_cap_f")
                    route_power = number(item, "delta_switching_power_w")
                    placed_power += place_power
                    routed_signal += route_power
                    per_net_rows.append({
                        "candidate": candidate, "workload": workload, "net": item["net"],
                        "driver_pin": pin, "activity_density_hz": density,
                        "delta_place_wire_cap_f": place_delta_cap,
                        "delta_route_wire_cap_f": route_delta_cap,
                        "raw_place_power_prediction_w": place_power,
                        "pico_scaled_cap_power_prediction_w": cap_scale * place_power,
                        "routed_signal_power_delta_w": route_power,
                    })
                placement_audit = number(audit[workload][candidate], "predicted_delta_w")
                placement_reconciliation = abs(placed_power - placement_audit)
                if placement_reconciliation > 1e-9:
                    raise ValueError(
                        f"placed per-net score does not reconcile for {candidate}/{workload}: "
                        f"{placement_reconciliation:g} W"
                    )
                route = route_map[(candidate, workload)]
                if abs(routed_signal * 1e9 - number(route, "signal_net_nw")) > 0.01:
                    raise ValueError(f"routed signal attribution does not reconcile for {candidate}/{workload}")
                candidate_rows.append({
                    "candidate": candidate, "workload": workload,
                    "timing_safe_50ps": int(
                        number(route, "setup_loss_ns") >= -0.05
                        and number(route, "hold_loss_ns") >= -0.05
                    ),
                    "placement_audit_prediction_w": placement_audit,
                    "placement_signal_prediction_w": placed_power,
                    "routed_signal_delta_w": routed_signal,
                    "routed_clock_delta_w": number(route, "clock_net_nw") * 1e-9,
                    "routed_total_delta_w": number(route, "routed_delta_nw") * 1e-9,
                    "setup_loss_ns": number(route, "setup_loss_ns"),
                    "hold_loss_ns": number(route, "hold_loss_ns"),
                    "placement_reconciliation_error_w": placement_reconciliation,
                    "zero_signal_prediction_w": 0.0,
                    "pico_workload_mean_signal_prediction_w": pico_signal_mean[workload],
                    "pico_scaled_cap_signal_prediction_w": cap_scale * placed_power,
                    "pico_scaled_power_signal_prediction_w": power_scale * placed_power,
                    "pico_workload_mean_total_prediction_w": pico_total_mean[workload],
                })

        write_table(output_dir / "per_net_predictions.tsv", per_net_rows)
        write_table(output_dir / "candidate_workload_predictions.tsv", candidate_rows)
        safe = [row for row in candidate_rows if int(row["timing_safe_50ps"]) == 1]
        metrics: list[dict[str, object]] = []
        for scope, group in (("routed timing-safe", safe), ("all routed probes", candidate_rows)):
            for metric, actual_key, models in (
                ("signal power", "routed_signal_delta_w", {
                    "zero": "zero_signal_prediction_w",
                    "Pico workload mean": "pico_workload_mean_signal_prediction_w",
                    "raw placement": "placement_signal_prediction_w",
                    "Pico cap scale × placement": "pico_scaled_cap_signal_prediction_w",
                    "Pico power scale × placement": "pico_scaled_power_signal_prediction_w",
                }),
                ("full routed power including clock", "routed_total_delta_w", {
                    "zero": "zero_signal_prediction_w",
                    "Pico workload mean": "pico_workload_mean_total_prediction_w",
                    "raw placement": "placement_signal_prediction_w",
                    "Pico cap scale × placement": "pico_scaled_cap_signal_prediction_w",
                    "Pico power scale × placement": "pico_scaled_power_signal_prediction_w",
                }),
            ):
                actual = [number(row, actual_key) for row in group]
                for model, prediction_key in models.items():
                    predicted = [number(row, prediction_key) for row in group]
                    sign_matches, sign_cases = sign_score(actual, predicted)
                    metrics.append({
                        "scope": scope, "metric": metric, "model": model,
                        "rows": len(group), "mae_nw": mae(actual, predicted) * 1e9,
                        "sign_matches": sign_matches, "sign_cases": sign_cases,
                    })
        write_table(output_dir / "model_metrics.tsv", metrics)

        cap_safe = [row for row in per_net_rows if row["candidate"] in {r["candidate"] for r in safe}]
        cap_metrics: list[dict[str, object]] = []
        for scope, group in (("routed timing-safe", cap_safe), ("all routed probes", per_net_rows)):
            actual = [number(row, "delta_route_wire_cap_f") for row in group]
            raw = [number(row, "delta_place_wire_cap_f") for row in group]
            scaled = [cap_scale * value for value in raw]
            for model, predicted in (("zero", [0.0] * len(group)), ("raw placement", raw), ("Pico cap scale", scaled)):
                matches, cases = sign_score(actual, predicted)
                cap_metrics.append({
                    "scope": scope, "metric": "per-net routed wire-cap delta",
                    "model": model, "rows": len(group), "mae_ff": mae(actual, predicted) / 1e-15,
                    "sign_matches": matches, "sign_cases": cases,
                })
        write_table(output_dir / "wirecap_metrics.tsv", cap_metrics)

        all_reductions = []
        for candidate in candidates:
            group = [row for row in safe if row["candidate"] == candidate]
            if len(group) == len(WORKLOADS) and all(number(row, "routed_total_delta_w") < 0 for row in group):
                all_reductions.append(candidate)
        signal_raw = next(row for row in metrics if row["scope"] == "routed timing-safe" and row["metric"] == "signal power" and row["model"] == "raw placement")
        signal_zero = next(row for row in metrics if row["scope"] == "routed timing-safe" and row["metric"] == "signal power" and row["model"] == "zero")
        full_raw = next(row for row in metrics if row["scope"] == "routed timing-safe" and row["metric"] == "full routed power including clock" and row["model"] == "raw placement")
        full_zero = next(row for row in metrics if row["scope"] == "routed timing-safe" and row["metric"] == "full routed power including clock" and row["model"] == "zero")
        max_reconciliation = max(number(row, "placement_reconciliation_error_w") for row in candidate_rows)
        lines = [
            "# Expanded GCD placement-to-routed calibration",
            "",
            f"Routed probes: {len(candidates)}; 50 ps timing-safe probes: {len({row['candidate'] for row in safe})}.",
            f"Timing-safe candidate/workload rows: {len(safe)}/{len(candidate_rows)}; all-workload routed total-power reductions: {len(all_reductions)}.",
            f"Maximum placed per-net score reconciliation error: {max_reconciliation * 1e9:.6f} nW.",
            f"PicoRV32 trained scales: routed ΔC = {cap_scale:.6g} × placed ΔC; signal power = {power_scale:.6g} × raw placement estimate.",
            "",
            "## Prediction errors on routed timing-safe cases",
            "",
            "| Target | Model | MAE | Sign agreement |",
            "|---|---|---:|---:|",
        ]
        for row in metrics:
            if row["scope"] != "routed timing-safe":
                continue
            unit = "nW" if row["metric"] in {"signal power", "full routed power including clock"} else ""
            lines.append(
                f"| {row['metric']} | {row['model']} | {number(row, 'mae_nw'):.3f} {unit} | "
                f"{row['sign_matches']}/{row['sign_cases']} |"
            )
        lines.extend([
            "",
            "## Per-net routed wire-capacitance prediction",
            "",
            "| Model | MAE | Sign agreement |",
            "|---|---:|---:|",
        ])
        for row in cap_metrics:
            if row["scope"] == "routed timing-safe":
                lines.append(
                    f"| {row['model']} | {number(row, 'mae_ff'):.6f} fF | "
                    f"{row['sign_matches']}/{row['sign_cases']} |"
                )
        lines.extend([
            "",
            "## Interpretation",
            "",
            f"For signal-net power, raw placement MAE is {number(signal_raw, 'mae_nw'):.3f} nW versus "
            f"{number(signal_zero, 'mae_nw'):.3f} nW for zero-change prediction. For full routed power, "
            f"raw placement MAE is {number(full_raw, 'mae_nw'):.3f} nW versus {number(full_zero, 'mae_nw'):.3f} nW for zero-change prediction.",
            "Clock-tree deltas are reported separately because CTS is downstream of placement; they are not included in the placement signal-power model.",
            f"Timing-safe probes that reduce routed total power in all three workloads: {len(all_reductions)}. The probe set is diagnostic and does not establish a successful optimization.",
            "The ten routed probes were stratified across the placement prediction range, so these errors describe this calibration sweep rather than an unbiased random sample.",
            "",
            "Per-net predictions, candidate/workload predictions, and model tables are saved beside this report.",
        ])
        report = output_dir / "expanded_calibration_report.md"
        report.write_text("\n".join(lines) + "\n")
        print(report.read_text(), end="")

        inputs = [
            args.candidate_selection.resolve(), args.audit_dir / "audit_idle.tsv",
            args.audit_dir / "audit_typical.tsv", args.audit_dir / "audit_peak.tsv",
            GCD_BASELINE / "3_5_place_dp.odb", GCD_BASELINE / "3_place.sdc",
            PLACE_REPORT, OPENROAD, PICO_ANALYSIS / "unified_per_net_features.csv",
            PICO_ANALYSIS / "candidate_workload_attribution.tsv",
        ]
        inputs.extend(ACTIVITY_ROOT / f"{workload}.vcd" for workload in WORKLOADS)
        for candidate in candidates:
            inputs.append(output_dir / "candidate_odb" / f"{candidate}.odb")
            for workload in WORKLOADS:
                inputs.append(args.route_root / f"gcd_cal_{candidate}_{args.tag}" / "net_attribution" / f"{workload}.per_net_delta.csv")
        hash_rows = [{"path": str(path), "sha256": sha256(path)} for path in sorted(set(inputs))]
        write_table(output_dir / "analysis_input_hashes.tsv", hash_rows)
        return 0
    except (OSError, KeyError, ValueError, RuntimeError, StopIteration) as exc:
        print(f"expanded GCD calibration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
