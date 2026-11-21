#!/usr/bin/env python3
"""Join workload-specific placement trials and retain robust Pareto moves."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
RESTORE_RE = re.compile(
    r"MOVE_EVAL_RESTORED switching_error=(?P<power>[-+0-9.eE]+) W "
    r"setup_error=(?P<setup>[-+0-9.eE]+) ns hold_error=(?P<hold>[-+0-9.eE]+) ns"
)
BASELINE_RE = re.compile(r"MOVE_EVAL_BASELINE .* error=(?P<error>[-+0-9.eE]+) W")
KEY_FIELDS = ("instance", "from_x", "from_y", "to_x", "to_y")
MEASURED = {"evaluated", "timing_regression"}


def move_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row[field] for field in KEY_FIELDS)


def read_rows(path: Path) -> dict[tuple[str, ...], dict[str, str]]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    indexed: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        key = move_key(row)
        if key in indexed:
            raise ValueError(f"duplicate move key in {path}: {key}")
        indexed[key] = row
    return indexed


def read_checks(path: Path) -> tuple[float, float, float, float]:
    text = path.read_text(errors="replace")
    restore = RESTORE_RE.search(text)
    baseline = BASELINE_RE.search(text)
    if restore is None or baseline is None:
        raise ValueError(f"{path} is missing baseline or restore checks")
    return (
        float(restore.group("power")),
        float(restore.group("setup")),
        float(restore.group("hold")),
        float(baseline.group("error")),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--move-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--pareto", type=Path, required=True)
    parser.add_argument("--nonregression-tolerance-w", type=float, default=1.0e-12)
    parser.add_argument("--max-delta-error-w", type=float, default=1.0e-10)
    parser.add_argument("--max-restore-error-w", type=float, default=1.0e-9)
    args = parser.parse_args()

    try:
        rows_by_workload = {
            workload: read_rows(args.move_dir / f"candidate_moves_{workload}.tsv")
            for workload in WORKLOADS
        }
        checks = {
            workload: read_checks(args.move_dir / f"evaluator_{workload}.log")
            for workload in WORKLOADS
        }
    except (OSError, ValueError, KeyError) as exc:
        print(f"multi-workload move validation failed: {exc}", file=sys.stderr)
        return 2

    key_sets = {workload: set(rows) for workload, rows in rows_by_workload.items()}
    reference_keys = key_sets[WORKLOADS[0]]
    if any(keys != reference_keys for keys in key_sets.values()):
        differences = {
            workload: {
                "missing": len(reference_keys - key_sets[workload]),
                "extra": len(key_sets[workload] - reference_keys),
            }
            for workload in WORKLOADS[1:]
            if key_sets[workload] != reference_keys
        }
        print(f"workloads did not evaluate the same move proposals: {differences}", file=sys.stderr)
        return 2

    measured_keys = [
        key
        for key in sorted(reference_keys)
        if all(rows_by_workload[workload][key]["status"] in MEASURED for workload in WORKLOADS)
    ]
    timing_safe_keys = [
        key
        for key in measured_keys
        if all(rows_by_workload[workload][key]["timing_ok"] == "1" for workload in WORKLOADS)
    ]
    nonregressing_keys = [
        key
        for key in timing_safe_keys
        if all(
            float(rows_by_workload[workload][key]["observed_delta_w"])
            <= args.nonregression_tolerance_w
            for workload in WORKLOADS
        )
    ]

    # Treat only sub-picowatt positive deltas as numerical zero when building
    # the Pareto frontier. Switching deltas themselves remain unrounded in TSV.
    vectors: dict[tuple[str, ...], tuple[float, float, float]] = {}
    for key in nonregressing_keys:
        values = []
        for workload in WORKLOADS:
            value = float(rows_by_workload[workload][key]["observed_delta_w"])
            values.append(0.0 if abs(value) <= args.nonregression_tolerance_w else value)
        vectors[key] = tuple(values)  # type: ignore[assignment]

    pareto_keys = []
    for key, vector in vectors.items():
        dominated = any(
            other_key != key
            and all(other <= own for other, own in zip(other_vector, vector))
            and any(other < own for other, own in zip(other_vector, vector))
            for other_key, other_vector in vectors.items()
        )
        if not dominated:
            pareto_keys.append(key)

    pareto_path = args.pareto
    pareto_path.parent.mkdir(parents=True, exist_ok=True)
    pareto_fields = [
        "instance", "dx_dbu", "from_x", "from_y", "to_x", "to_y",
        "idle_delta_w", "typical_delta_w", "peak_delta_w",
        "idle_setup_loss_ps", "typical_setup_loss_ps", "peak_setup_loss_ps",
        "idle_hold_loss_ps", "typical_hold_loss_ps", "peak_hold_loss_ps",
    ]
    with pareto_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, delimiter="\t", fieldnames=pareto_fields)
        writer.writeheader()
        for key in sorted(pareto_keys):
            row_base = rows_by_workload["typical"][key]
            record = {
                "instance": row_base["instance"],
                "dx_dbu": row_base["dx_dbu"],
                "from_x": row_base["from_x"],
                "from_y": row_base["from_y"],
                "to_x": row_base["to_x"],
                "to_y": row_base["to_y"],
            }
            for workload in WORKLOADS:
                row = rows_by_workload[workload][key]
                record[f"{workload}_delta_w"] = row["observed_delta_w"]
                record[f"{workload}_setup_loss_ps"] = (
                    f"{(float(row['setup_before_ns']) - float(row['setup_after_ns'])) * 1000.0:.6f}"
                )
                record[f"{workload}_hold_loss_ps"] = (
                    f"{(float(row['hold_before_ns']) - float(row['hold_after_ns'])) * 1000.0:.6f}"
                )
            writer.writerow(record)

    measured_errors = [
        abs(float(rows_by_workload[workload][key]["delta_error_w"]))
        for workload in WORKLOADS
        for key in measured_keys
    ]
    max_delta_error = max(measured_errors, default=0.0)
    illegal_counts = {
        workload: sum(row["status"] == "illegal" for row in rows.values())
        for workload, rows in rows_by_workload.items()
    }
    restore_max = {
        field: max(checks[workload][index] for workload in WORKLOADS)
        for index, field in enumerate(("power", "setup", "hold", "baseline formula"))
    }

    summary = [
        f"Move proposals evaluated per workload: {len(reference_keys)}",
        f"Legal, measured in all workloads: {len(measured_keys)}",
        f"Timing-safe in all workloads (setup and hold loss <= 50 ps): {len(timing_safe_keys)}",
        f"Non-regressing in all workloads (delta <= {args.nonregression_tolerance_w:.3e} W): {len(nonregressing_keys)}",
        f"Pareto candidates across idle/typical/peak switching power: {len(pareto_keys)}",
        "Illegal move counts: " + ", ".join(f"{w}={illegal_counts[w]}" for w in WORKLOADS),
        f"Maximum |predicted - observed switching delta| across measured rows: {max_delta_error:.3e} W",
        "Largest restore errors across workloads: "
        f"power={restore_max['power']:.3e} W, setup={restore_max['setup']:.3e} ns, "
        f"hold={restore_max['hold']:.3e} ns",
        f"Largest baseline formula reconciliation error: {restore_max['baseline formula']:.3e} W",
        f"Pareto table: {pareto_path}",
        "Measurement stage: placement-estimated parasitics; promising candidates require routed SPEF validation.",
    ]
    for key in pareto_keys:
        vector = vectors[key]
        summary.append(
            f"Pareto move: {key[0]} ({key[1]},{key[2]}) -> ({key[3]},{key[4]}) DBU; "
            + ", ".join(f"{w}={vector[i]:+.3e} W" for i, w in enumerate(WORKLOADS))
        )
    if not pareto_keys:
        summary.append("Pareto move: none met all-workload non-regression and timing guards")

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(summary) + "\n")
    print("\n".join(summary))

    failures = []
    if max_delta_error > args.max_delta_error_w:
        failures.append("candidate delta reconciliation exceeded tolerance")
    if restore_max["power"] > args.max_restore_error_w or restore_max["setup"] > 1.0e-6 or restore_max["hold"] > 1.0e-6:
        failures.append("a workload did not restore the baseline metrics")
    if restore_max["baseline formula"] > 1.0e-8:
        failures.append("a workload's activity-cost formula did not reconcile with OpenSTA")
    if failures:
        print("multi-workload validation failed: " + "; ".join(failures), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
