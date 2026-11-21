#!/usr/bin/env python3
"""Summarize an isolated batch of placement-to-routed-SPEF calibrations."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


POWER_RE = re.compile(
    r"^- (?P<workload>idle|typical|peak): switching .*?\(delta (?P<switching>[+-]?[0-9.eE+-]+) W\); "
    r"total .*?\(delta (?P<total>[+-]?[0-9.eE+-]+) W\)\."
)
CAL_RE = re.compile(
    r"Matched non-clock nets: (?P<matched>\d+); nonzero placement predictions: (?P<placed>\d+); "
    r"nonzero routed changes: (?P<routed>\d+);"
)


def read_power(path: Path) -> tuple[dict[str, tuple[float, float]], bool, bool]:
    text = path.read_text(errors="replace")
    power: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        match = POWER_RE.match(line)
        if match:
            power[match.group("workload")] = (
                float(match.group("switching")), float(match.group("total"))
            )
    if set(power) != {"idle", "typical", "peak"}:
        raise ValueError(f"incomplete routed workload comparison in {path}")
    return (
        power,
        "Setup/hold guard (50 ps): PASS." in text,
        "SPEF candidate acceptance: PASS." in text,
    )


def read_predictions(path: Path) -> dict[str, float]:
    with path.open(newline="") as source:
        row = next(csv.DictReader(source, delimiter="\t"))
    return {
        workload: float(row[f"{workload}_predicted_delta_w"])
        for workload in ("idle", "typical", "peak")
    }


def read_calibration_counts(path: Path) -> dict[str, tuple[int, int, int]]:
    text = path.read_text(errors="replace")
    results: dict[str, tuple[int, int, int]] = {}
    current = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped in {"idle:", "typical:", "peak:"}:
            current = stripped[:-1]
        elif current:
            match = CAL_RE.search(stripped)
            if match:
                results[current] = tuple(int(match.group(name)) for name in ("matched", "placed", "routed"))
                current = ""
    if set(results) != {"idle", "typical", "peak"}:
        raise ValueError(f"incomplete per-net calibration summary in {path}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.batch_dir / "batch_manifest.tsv"
    try:
        with manifest_path.open(newline="") as source:
            candidates = list(csv.DictReader(source, delimiter="\t"))
        if not candidates:
            raise ValueError("batch manifest contains no candidates")
        lines = [
            "Placement-to-routed-SPEF calibration batch",
            "Power deltas are candidate minus the shared baseline; positive means power increased.",
            "",
            "| Move | Placement estimate idle/typical/peak (nW) | Routed switching delta idle/typical/peak (nW) | Timing | Accepted |",
            "|---|---:|---:|---|---|",
        ]
        all_cal: dict[str, list[tuple[int, int, int]]] = {w: [] for w in ("idle", "typical", "peak")}
        for candidate in candidates:
            route_report = args.batch_dir.parent / candidate["route_variant"] / "summary.txt"
            calibration_report = Path(candidate["calibration_report"]) / "summary.txt"
            placement_odb = Path(candidate["placement"])
            predictions = read_predictions(placement_odb.parent / "applied_move.tsv")
            routed, timing_pass, accepted = read_power(route_report)
            counts = read_calibration_counts(calibration_report)
            for workload in all_cal:
                all_cal[workload].append(counts[workload])
            move = f"{candidate['instance']} {candidate['dx_dbu']} DBU"
            pred = "/".join(f"{predictions[w] * 1e9:+.2f}" for w in ("idle", "typical", "peak"))
            delta = "/".join(f"{routed[w][0] * 1e9:+.2f}" for w in ("idle", "typical", "peak"))
            lines.append(
                f"| {move} | {pred} | {delta} | {'PASS' if timing_pass else 'FAIL'} | "
                f"{'PASS' if accepted else 'REJECTED'} |"
            )

        lines.extend(["", "Per-net calibration (matched non-clock nets):"])
        for workload, rows in all_cal.items():
            matched = {row[0] for row in rows}
            if len(matched) != 1:
                raise ValueError(f"matched net counts disagree for {workload}: {sorted(matched)}")
            placed = ", ".join(str(row[1]) for row in rows)
            routed_counts = ", ".join(str(row[2]) for row in rows)
            lines.append(
                f"- {workload}: {next(iter(matched))} matched; meaningful placement predictions per move "
                f"[{placed}]; routed net changes per move [{routed_counts}]."
            )

        integrity_path = args.batch_dir / "integrity.tsv"
        integrity = dict(
            line.split("\t", 1) for line in integrity_path.read_text().splitlines() if "\t" in line
        )
        same_hash = integrity.get("baseline_place_odb_sha256_before") == integrity.get(
            "baseline_place_odb_sha256_after"
        )
        lines.extend(
            [
                "",
                f"Baseline placement ODB integrity: {'PASS' if same_hash else 'FAIL'}.",
                f"Baseline placement ODB SHA-256: {integrity.get('baseline_place_odb_sha256_after', 'missing')}.",
                "Interpretation: compare placement-stage estimates with routed changes across candidates and workloads; these results are calibration data, not a general correction model.",
                "Use held-out candidate moves to check any future feature model against the zero-change baseline before adding a placer cost term.",
            ]
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(lines) + "\n")
        print(args.output.read_text(), end="")
        return 0 if same_hash else 2
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"batch summary failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
