#!/usr/bin/env python3
"""Summarize matched baseline/candidate routed SPEF power and timing reports."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


TOTAL_RE = re.compile(
    r"^Total\s+(?P<internal>[-+0-9.eE]+)\s+(?P<switching>[-+0-9.eE]+)\s+"
    r"(?P<leakage>[-+0-9.eE]+)\s+(?P<total>[-+0-9.eE]+)",
    re.MULTILINE,
)
METRICS_RE = re.compile(
    r"SPEF_METRICS workload=(?P<workload>\S+) switching_w=(?P<switching>[-+0-9.eE]+) "
    r"setup_ns=(?P<setup>[-+0-9.eE]+) hold_ns=(?P<hold>[-+0-9.eE]+)"
)
ANNOTATED_RE = re.compile(r"^Annotated\s+(?P<count>\d+) pin activities\.", re.MULTILINE)
UNANNOTATED_RE = re.compile(r"^unannotated\s+(?P<count>\d+)", re.MULTILINE)


def read_report(path: Path, workload: str) -> dict[str, float | int]:
    text = path.read_text(errors="replace")
    power = TOTAL_RE.search(text)
    metrics = METRICS_RE.search(text)
    annotated = ANNOTATED_RE.search(text)
    unannotated = UNANNOTATED_RE.search(text)
    if not all((power, metrics, annotated, unannotated)):
        raise ValueError(f"incomplete SPEF power/activity report in {path}")
    if metrics.group("workload") != workload:
        raise ValueError(f"wrong workload in {path}: {metrics.group('workload')}")
    values = {key: float(value) for key, value in power.groupdict().items()}
    values.update(
        switching_metric=float(metrics.group("switching")),
        setup=float(metrics.group("setup")),
        hold=float(metrics.group("hold")),
        annotated=int(annotated.group("count")),
        unannotated=int(unannotated.group("count")),
    )
    if values["annotated"] == 0:
        raise ValueError(f"no VCD activity was annotated in {path}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--route-marker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-timing-loss-ns", type=float, default=0.05)
    parser.add_argument("--max-leakage-change-w", type=float, default=1.0e-9)
    args = parser.parse_args()

    routed = args.route_marker.is_file() and args.route_marker.stat().st_size > 0
    lines = [
        "Routed SPEF candidate comparison",
        f"Candidate routing completed: {'PASS' if routed else 'FAIL'}",
        "Baseline and candidate use their own final netlist VCD, final ODB, final SDC, and extracted SPEF.",
        "",
        "Workload comparison (negative delta means lower candidate power):",
    ]
    typical_dynamic_improved = False
    idle_peak_no_dynamic_regression = True
    timing_safe = True
    leakage_invariant = True
    activity_complete = True
    for workload in ("idle", "typical", "peak"):
        base = read_report(args.baseline_dir / f"{workload}.log", workload)
        cand = read_report(args.candidate_dir / f"{workload}.log", workload)
        delta_internal = float(cand["internal"]) - float(base["internal"])
        delta_switching = float(cand["switching"]) - float(base["switching"])
        delta_dynamic = delta_internal + delta_switching
        delta_leakage = float(cand["leakage"]) - float(base["leakage"])
        delta_total = float(cand["total"]) - float(base["total"])
        setup_loss = float(cand["setup"]) - float(base["setup"])
        hold_loss = float(cand["hold"]) - float(base["hold"])
        timing_ok = setup_loss >= -args.max_timing_loss_ns and hold_loss >= -args.max_timing_loss_ns
        timing_safe = timing_safe and timing_ok
        leakage_invariant = leakage_invariant and abs(delta_leakage) <= args.max_leakage_change_w
        workload_activity_complete = (
            int(base["unannotated"]) == 0
            and int(cand["unannotated"]) == 0
        )
        activity_complete = activity_complete and workload_activity_complete
        lines.append(
            f"- {workload}: internal {float(base['internal']):.12e} -> "
            f"{float(cand['internal']):.12e} W (delta {delta_internal:+.12e} W); "
            f"net switching {float(base['switching']):.12e} -> "
            f"{float(cand['switching']):.12e} W (delta {delta_switching:+.12e} W); "
            f"dynamic (internal + net switching) delta {delta_dynamic:+.12e} W; "
            f"leakage delta {delta_leakage:+.12e} W; "
            f"total {float(base['total']):.12e} -> {float(cand['total']):.12e} W "
            f"(delta {delta_total:+.12e} W)."
        )
        lines.append(
            f"  Timing slack: setup {float(base['setup']):.6f} -> {float(cand['setup']):.6f} ns; "
            f"hold {float(base['hold']):.6f} -> {float(cand['hold']):.6f} ns. "
            f"Activity annotated: {int(base['annotated'])} baseline / {int(cand['annotated'])} candidate; "
            f"unannotated: {int(base['unannotated'])} / {int(cand['unannotated'])}."
        )
        if workload == "typical":
            typical_dynamic_improved = delta_dynamic < 0.0
        elif delta_dynamic > 0.0:
            idle_peak_no_dynamic_regression = False

    accepted = (
        routed
        and typical_dynamic_improved
        and idle_peak_no_dynamic_regression
        and timing_safe
        and leakage_invariant
        and activity_complete
    )
    lines.extend(
        [
            "",
            f"Typical internal + net switching dynamic power improved: {'PASS' if typical_dynamic_improved else 'FAIL'}.",
            f"Idle/peak internal + net switching dynamic power did not regress: {'PASS' if idle_peak_no_dynamic_regression else 'FAIL'}.",
            f"Leakage stayed within {args.max_leakage_change_w:.3e} W: {'PASS' if leakage_invariant else 'FAIL'}.",
            f"VCD annotation coverage complete: {'PASS' if activity_complete else 'FAIL'}.",
            f"Setup/hold guard ({args.max_timing_loss_ns * 1000:.0f} ps): {'PASS' if timing_safe else 'FAIL'}.",
            f"SPEF candidate acceptance: {'PASS' if accepted else 'REJECTED'}.",
        ]
    )
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output.read_text(), end="")
    return 0 if accepted else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print(f"SPEF comparison failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
