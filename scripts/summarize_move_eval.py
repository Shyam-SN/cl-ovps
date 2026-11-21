#!/usr/bin/env python3
"""Validate and summarize temporary placement-move power trials."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


RESTORE_RE = re.compile(
    r"MOVE_EVAL_RESTORED switching_error=(?P<power>[-+0-9.eE]+) W "
    r"setup_error=(?P<setup>[-+0-9.eE]+) ns hold_error=(?P<hold>[-+0-9.eE]+) ns"
)
BASELINE_RE = re.compile(r"MOVE_EVAL_BASELINE .* error=(?P<error>[-+0-9.eE]+) W")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--moves", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-delta-error-w", type=float, default=1.0e-10)
    parser.add_argument("--max-restore-error-w", type=float, default=1.0e-9)
    args = parser.parse_args()

    with args.moves.open(newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    measured = [row for row in rows if row["status"] in {"evaluated", "timing_regression"}]
    if not measured:
        raise SystemExit("no legal move candidates were measured")

    max_delta_error = max(abs(float(row["delta_error_w"])) for row in measured)
    timing_safe = [row for row in measured if row["timing_ok"] == "1"]
    reducing = [
        row
        for row in timing_safe
        if float(row["predicted_delta_w"]) < 0.0
        and float(row["observed_delta_w"]) < 0.0
    ]
    best = min(reducing, key=lambda row: float(row["observed_delta_w"])) if reducing else None

    log = args.log.read_text(errors="replace")
    restore = RESTORE_RE.search(log)
    baseline = BASELINE_RE.search(log)
    if restore is None or baseline is None:
        raise SystemExit("evaluator log is missing baseline or restore checks")
    restore_power = float(restore.group("power"))
    restore_setup = float(restore.group("setup"))
    restore_hold = float(restore.group("hold"))
    baseline_error = float(baseline.group("error"))

    summary = [
        f"Measured legal candidates: {len(measured)}",
        f"Timing-safe candidates: {len(timing_safe)}",
        f"Rejected illegal candidates: {sum(row['status'] == 'illegal' for row in rows)}",
        f"Maximum |predicted - observed switching delta|: {max_delta_error:.3e} W",
        f"Baseline formula reconciliation error: {baseline_error:.3e} W",
        f"Restore errors: power={restore_power:.3e} W, setup={restore_setup:.3e} ns, hold={restore_hold:.3e} ns",
    ]
    if best:
        summary.append(
            "Best timing-safe reducing move: "
            f"{best['instance']} dx={best['dx_dbu']} DBU; "
            f"predicted={float(best['predicted_delta_w']):.3e} W, "
            f"observed={float(best['observed_delta_w']):.3e} W"
        )
    else:
        summary.append("Best timing-safe reducing move: none found in this candidate set")

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(summary) + "\n")
    print("\n".join(summary))

    failures = []
    if max_delta_error > args.max_delta_error_w:
        failures.append("candidate delta reconciliation exceeded tolerance")
    if restore_power > args.max_restore_error_w or restore_setup > 1.0e-6 or restore_hold > 1.0e-6:
        failures.append("checkpoint restoration did not reproduce baseline metrics")
    if baseline_error > 1.0e-8:
        failures.append("baseline activity-cost score did not reconcile with OpenSTA")
    if failures:
        print("move-evaluation validation failed: " + "; ".join(failures), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
