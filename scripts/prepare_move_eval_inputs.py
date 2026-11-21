#!/usr/bin/env python3
"""Prepare the union of high-cost cells and activity maps for all workloads."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")


def read_cost_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows:
        raise SystemExit(f"no cost rows found in {path}")
    rows.sort(key=lambda row: float(row["estimated_switching_power_w"]), reverse=True)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for workload in WORKLOADS:
        parser.add_argument(f"--{workload}-cost-csv", type=Path, required=True)
    parser.add_argument("--candidate-file", type=Path, required=True)
    parser.add_argument("--activity-dir", type=Path, required=True)
    parser.add_argument("--max-cells", type=int, default=6)
    args = parser.parse_args()
    if args.max_cells < 1:
        parser.error("--max-cells must be positive")

    candidates: dict[str, dict[str, str]] = {}
    workload_rows: dict[str, list[dict[str, str]]] = {}
    selected_by_workload: dict[str, list[str]] = {}
    for workload in WORKLOADS:
        rows = read_cost_rows(getattr(args, f"{workload}_cost_csv"))
        workload_rows[workload] = rows
        selected: list[str] = []
        seen: set[str] = set()
        for row in rows:
            pin = row["pin"]
            if "/" not in pin:
                continue
            instance = pin.rsplit("/", 1)[0]
            if instance in seen:
                continue
            seen.add(instance)
            selected.append(instance)
            existing = candidates.get(instance)
            if existing is None or float(row["estimated_switching_power_w"]) > float(existing["seed_cost_w"]):
                candidates[instance] = {
                    "instance": instance,
                    "seed_workload": workload,
                    "seed_pin": pin,
                    "seed_net": row["net"],
                    "seed_cost_w": row["estimated_switching_power_w"],
                }
            if len(selected) == args.max_cells:
                break
        selected_by_workload[workload] = selected

    if not candidates:
        raise SystemExit("could not derive candidate instances from the workload cost reports")

    args.candidate_file.parent.mkdir(parents=True, exist_ok=True)
    args.activity_dir.mkdir(parents=True, exist_ok=True)
    with args.candidate_file.open("w", newline="") as output:
        writer = csv.DictWriter(
            output,
            delimiter="\t",
            fieldnames=["instance", "seed_workload", "seed_pin", "seed_net", "seed_cost_w"],
        )
        writer.writeheader()
        writer.writerows(sorted(candidates.values(), key=lambda row: row["instance"]))

    for workload, rows in workload_rows.items():
        # The per-net table is OpenSTA-matched. A net must have one consistent
        # transition density within a workload before it can score a move.
        net_density: dict[str, float] = {}
        for row in rows:
            net = row["net"]
            density = float(row["density_hz"])
            previous = net_density.get(net)
            if previous is not None and abs(previous - density) > max(1e-3, density * 1e-6):
                raise SystemExit(f"inconsistent {workload} activity for net {net}: {previous} vs {density}")
            net_density[net] = density

        activity_path = args.activity_dir / f"{workload}.active_net_density.tsv"
        with activity_path.open("w", newline="") as output:
            writer = csv.writer(output, delimiter="\t", lineterminator="\n")
            writer.writerow(["net", "density_hz"])
            for net, density in sorted(net_density.items()):
                writer.writerow([net, f"{density:.9g}"])
        print(
            f"{workload}: selected {len(selected_by_workload[workload])} cells; "
            f"wrote {len(net_density)} active nets to {activity_path}"
        )

    print(f"Union candidate cells: {len(candidates)} (up to {args.max_cells} per workload)")
    for candidate in sorted(candidates.values(), key=lambda row: row["instance"]):
        print(
            f"  {candidate['instance']} seed={candidate['seed_workload']}:{candidate['seed_net']} "
            f"cost={float(candidate['seed_cost_w']):.6e} W"
        )
    print(f"Candidate list: {args.candidate_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
