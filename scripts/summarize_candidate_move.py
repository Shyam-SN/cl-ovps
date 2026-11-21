#!/usr/bin/env python3
"""Compare one saved placement move against its detailed-place baseline."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


TOTAL_RE = re.compile(
    r"^Total\s+(?P<internal>[-+0-9.eE]+)\s+(?P<switching>[-+0-9.eE]+)\s+"
    r"(?P<leakage>[-+0-9.eE]+)\s+(?P<total>[-+0-9.eE]+)",
    re.MULTILINE,
)
METRICS_RE = re.compile(
    r"CANDIDATE_METRICS workload=(?P<workload>\S+) legal=(?P<legal>\S+) "
    r"switching_w=(?P<switching>[-+0-9.eE]+) setup_ns=(?P<setup>[-+0-9.eE]+) "
    r"hold_ns=(?P<hold>[-+0-9.eE]+)"
)


def read_power(path: Path) -> dict[str, float]:
    match = TOTAL_RE.search(path.read_text(errors="replace"))
    if not match:
        raise ValueError(f"could not parse Total power row in {path}")
    return {name: float(value) for name, value in match.groupdict().items()}


def read_candidate_metrics(path: Path, workload: str) -> dict[str, float | str]:
    text = path.read_text(errors="replace")
    match = METRICS_RE.search(text)
    if not match:
        raise ValueError(f"candidate metrics missing from {path}")
    values = match.groupdict()
    if values["workload"] != workload or values["legal"] != "pass":
        raise ValueError(f"candidate legality/workload check failed in {path}: {values}")
    return {
        "switching": float(values["switching"]),
        "setup": float(values["setup"]),
        "hold": float(values["hold"]),
    }


def read_eval_error(path: Path) -> float:
    maximum = 0.0
    with path.open(newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            if row["status"] in {"evaluated", "timing_regression"}:
                maximum = max(maximum, abs(float(row["delta_error_w"])))
    if maximum == 0.0:
        raise ValueError(f"no measured candidate delta errors in {path}")
    return maximum


def read_eval_errors(move_dir: Path, fallback_table: Path) -> dict[str, float]:
    tables = {
        workload: move_dir / f"candidate_moves_{workload}.tsv"
        for workload in ("idle", "typical", "peak")
    }
    if not all(path.is_file() for path in tables.values()):
        return {"all workloads": read_eval_error(fallback_table)}
    return {workload: read_eval_error(path) for workload, path in tables.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--move-summary", type=Path, required=True)
    parser.add_argument("--move-table", type=Path, required=True)
    parser.add_argument("--move-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    error_bounds = read_eval_errors(args.move_dir, args.move_table) if args.move_dir else {
        "all workloads": read_eval_error(args.move_table)
    }
    error_bound = max(error_bounds.values())
    move = next(csv.DictReader(args.move_summary.open(), delimiter="\t"))
    lines = [
        "One-move candidate verification",
        f"Candidate ODB: {move['candidate_odb']}",
        f"Move: {move['instance']} ({move['from_x']},{move['from_y']}) -> "
        f"({move['to_x']},{move['to_y']}) DBU",
        "Evaluator maximum delta error: "
        + ", ".join(f"{workload}={error:.6e} W" for workload, error in error_bounds.items()),
        "",
        "Workload power (negative delta means lower candidate power):",
    ]

    improving_workloads: list[str] = []
    regressions: list[str] = []
    for workload in ("idle", "typical", "peak"):
        base = read_power(args.baseline_dir / f"{workload}.log")
        candidate = read_power(args.candidate_dir / f"{workload}.log")
        metrics = read_candidate_metrics(args.candidate_dir / f"{workload}.log", workload)
        switching_delta = candidate["switching"] - base["switching"]
        total_delta = candidate["total"] - base["total"]
        relative = switching_delta / base["switching"] if base["switching"] else 0.0
        lines.append(
            f"- {workload}: switching {base['switching']:.9e} -> "
            f"{candidate['switching']:.9e} W (delta {switching_delta:+.9e} W, "
            f"{relative:+.4%}); total {base['total']:.9e} -> "
            f"{candidate['total']:.9e} W (delta {total_delta:+.9e} W)."
        )
        lines.append(
            f"  Legal: pass; setup slack {metrics['setup']:.6f} ns; "
            f"hold slack {metrics['hold']:.6f} ns."
        )
        if switching_delta < -error_bound:
            improving_workloads.append(workload)
        elif switching_delta > error_bound:
            regressions.append(workload)

    setup_loss = float(move["setup_after_ns"]) - float(move["setup_before_ns"])
    hold_loss = float(move["hold_after_ns"]) - float(move["hold_before_ns"])
    timing_pass = setup_loss >= -0.05 and hold_loss >= -0.05
    no_regression = not regressions
    power_improved = bool(improving_workloads)
    accepted = power_improved and timing_pass and no_regression
    lines.extend(
        [
            "",
            "At least one workload improves beyond evaluator error: "
            + (f"PASS ({', '.join(improving_workloads)})" if power_improved else "FAIL"),
            f"Setup/hold guard (50 ps maximum loss): {'PASS' if timing_pass else 'FAIL'}.",
            "Idle/peak regression check: "
            + ("PASS" if no_regression else f"FAIL ({', '.join(regressions)} regressed beyond error bound)"),
            f"Candidate acceptance: {'PASS' if accepted else 'REJECTED'}.",
            "The baseline ODB remains the reference; this candidate is saved separately.",
        ]
    )
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output.read_text(), end="")
    return 0 if accepted else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"candidate verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
