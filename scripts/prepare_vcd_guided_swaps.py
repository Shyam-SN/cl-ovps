#!/usr/bin/env python3
"""Pair high-activity cells with low-activity controls for legal placement swaps."""

from __future__ import annotations

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path

WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def excluded_instances(candidate_file: Path | None, batch_dirs: list[Path]) -> set[str]:
    excluded: set[str] = set()
    if candidate_file and candidate_file.exists():
        excluded.update(row["instance"] for row in read_tsv(candidate_file))
    for batch_dir in batch_dirs:
        manifest = batch_dir / "batch_manifest.tsv"
        if not manifest.exists():
            continue
        excluded.update(row["instance"] for row in read_tsv(manifest))
    return excluded


def read_activity_costs(paths: dict[str, Path]) -> dict[str, dict[str, float]]:
    # Sum output-net costs per cell and use load-capacitance-weighted toggle
    # density so the high/low classification reflects VCD activity, not area.
    totals: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(lambda: {"power": 0.0, "cap": 0.0, "density_cap": 0.0})
    )
    for workload, path in paths.items():
        with path.open(newline="") as source:
            for row in csv.DictReader(source):
                pin = row["pin"]
                if "/" not in pin:
                    continue
                instance = pin.rsplit("/", 1)[0]
                cap = max(0.0, float(row["load_cap_f"]))
                density = max(0.0, float(row["density_hz"]))
                metrics = totals[instance][workload]
                metrics["power"] += float(row["estimated_switching_power_w"])
                metrics["cap"] += cap
                metrics["density_cap"] += cap * density

    result: dict[str, dict[str, float]] = {}
    for instance, workload_values in totals.items():
        activity = [
            value["density_cap"] / value["cap"]
            for value in workload_values.values()
            if value["cap"] > 0
        ]
        loads = [value["cap"] for value in workload_values.values() if value["cap"] > 0]
        powers = [value["power"] for value in workload_values.values()]
        if not activity:
            continue
        result[instance] = {
            "activity_hz": max(activity),
            "typical_activity_hz": (
                workload_values["typical"]["density_cap"] / workload_values["typical"]["cap"]
                if workload_values["typical"]["cap"] > 0 else 0.0
            ),
            "load_cap_f": statistics.median(loads),
            "switching_power_w": max(powers),
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--idle-cost-csv", type=Path, required=True)
    parser.add_argument("--typical-cost-csv", type=Path, required=True)
    parser.add_argument("--peak-cost-csv", type=Path, required=True)
    parser.add_argument("--exclude-candidate-file", type=Path)
    parser.add_argument("--exclude-batch-dir", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-pairs", type=int, default=12)
    parser.add_argument("--high-percentile", type=float, default=75.0)
    parser.add_argument("--low-percentile", type=float, default=25.0)
    parser.add_argument("--min-activity-ratio", type=float, default=5.0)
    parser.add_argument("--max-cap-ratio", type=float, default=2.0)
    parser.add_argument("--allow-cross-row", action="store_true")
    args = parser.parse_args()
    if (args.max_pairs < 1 or not 0 <= args.low_percentile < args.high_percentile <= 100
            or args.min_activity_ratio <= 1.0 or args.max_cap_ratio < 1.0):
        parser.error("invalid pair count or activity percentiles")

    inventory_rows = read_tsv(args.inventory)
    inventory = {row["instance"]: row for row in inventory_rows}
    costs = read_activity_costs({
        "idle": args.idle_cost_csv,
        "typical": args.typical_cost_csv,
        "peak": args.peak_cost_csv,
    })
    excluded = excluded_instances(args.exclude_candidate_file, args.exclude_batch_dir)
    eligible = [
        {**inventory[name], **metrics}
        for name, metrics in costs.items()
        if name in inventory and name not in excluded
    ]
    if len(eligible) < 4:
        raise SystemExit(f"only {len(eligible)} eligible movable activity cells remain")

    ordered_activity = sorted(row["activity_hz"] for row in eligible)

    def percentile(values: list[float], rank: float) -> float:
        position = (len(values) - 1) * rank / 100.0
        low = math.floor(position)
        high = math.ceil(position)
        if low == high:
            return values[low]
        return values[low] + (values[high] - values[low]) * (position - low)

    high_cut = percentile(ordered_activity, args.high_percentile)
    low_cut = percentile(ordered_activity, args.low_percentile)
    high_cells = sorted(
        (row for row in eligible if row["activity_hz"] >= high_cut),
        key=lambda row: row["activity_hz"], reverse=True,
    )
    low_cells = [row for row in eligible if row["activity_hz"] <= low_cut]
    low_by_group: dict[tuple[str, str, str], list[dict[str, str | float]]] = defaultdict(list)
    for row in low_cells:
        key = (str(row["master"]), "*" if args.allow_cross_row else str(row["y_dbu"]), str(row["orient"]))
        low_by_group[key].append(row)
    for group in low_by_group.values():
        group.sort(key=lambda row: float(row["activity_hz"]))

    used_low: set[str] = set()
    used_high: set[str] = set()
    pairs: list[dict[str, str | float]] = []
    for high in high_cells:
        high_name = str(high["instance"])
        if high_name in used_high:
            continue
        group_key = (
            str(high["master"]),
            "*" if args.allow_cross_row else str(high["y_dbu"]),
            str(high["orient"]),
        )
        choices = []
        for low in low_by_group.get(group_key, []):
            low_name = str(low["instance"])
            if low_name in used_low or low_name == high_name:
                continue
            low_density = float(low["activity_hz"])
            high_density = float(high["activity_hz"])
            if low_density > 0 and high_density < args.min_activity_ratio * low_density:
                continue
            high_cap = float(high["load_cap_f"])
            low_cap = float(low["load_cap_f"])
            cap_ratio = max(high_cap, low_cap) / min(high_cap, low_cap) if min(high_cap, low_cap) > 0 else math.inf
            if cap_ratio > args.max_cap_ratio:
                continue
            distance = abs(int(high["x_dbu"]) - int(low["x_dbu"])) + abs(
                int(high["y_dbu"]) - int(low["y_dbu"])
            )
            choices.append((distance, cap_ratio, low_density, low))
        if not choices:
            continue
        distance, cap_ratio, low_density, low = min(
            choices,
            key=lambda item: (item[0], item[1], item[2])
            if args.allow_cross_row
            else (item[1], item[2], item[0]),
        )
        low_name = str(low["instance"])
        pair_id = f"swap{len(pairs) + 1:02d}"
        pairs.append({
            "pair_id": pair_id,
            "high_instance": high_name,
            "low_instance": low_name,
            "master": high["master"],
            "row_y_dbu": high["y_dbu"],
            "orient": high["orient"],
            "high_activity_hz": f"{float(high['activity_hz']):.9g}",
            "low_activity_hz": f"{low_density:.9g}",
            "activity_ratio": (
                f"{float(high['activity_hz']) / low_density:.6g}"
                if low_density > 0 else "inf"
            ),
            "high_switching_power_w": f"{float(high['switching_power_w']):.12e}",
            "low_switching_power_w": f"{float(low['switching_power_w']):.12e}",
            "high_load_cap_f": f"{float(high['load_cap_f']):.12e}",
            "low_load_cap_f": f"{float(low['load_cap_f']):.12e}",
            "load_cap_ratio": f"{cap_ratio:.6g}",
        })
        used_high.add(high_name)
        used_low.add(low_name)
        if len(pairs) >= args.max_pairs:
            break

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "pair_id", "high_instance", "low_instance", "master", "row_y_dbu", "orient",
        "high_activity_hz", "low_activity_hz", "activity_ratio",
        "high_switching_power_w", "low_switching_power_w",
        "high_load_cap_f", "low_load_cap_f", "load_cap_ratio",
    ]
    with args.output.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(pairs)

    summary = [
        f"Movable placed instances in inventory: {len(inventory_rows)}",
        f"VCD-scored instances present in inventory: {len(costs) - sum(name not in inventory for name in costs)}",
        f"Previously evaluated instances excluded: {len(excluded)}",
        f"Eligible instances after exclusions: {len(eligible)}",
        f"High-activity cutoff (p{args.high_percentile:g}): {high_cut:.6g} transitions/s",
        f"Low-activity cutoff (p{args.low_percentile:g}): {low_cut:.6g} transitions/s",
        f"High-activity eligible cells: {len(high_cells)}; low-activity controls: {len(low_cells)}",
        f"Matched same-master/same-orientation, similar-capacitance swaps: {len(pairs)}",
        f"Activity separation: at least {args.min_activity_ratio:g}x; load-capacitance ratio <= {args.max_cap_ratio:g}x.",
        f"Cross-row matching enabled: {args.allow_cross_row}.",
        "Swap candidates are proposals only; OpenROAD legality and timing checks run in the next stage.",
        f"Candidate table: {args.output}",
    ]
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(summary) + "\n")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
