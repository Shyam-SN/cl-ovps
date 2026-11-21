#!/usr/bin/env python3
"""Build a seeded pool of legal-site, local GCD flop relocation proposals."""

from __future__ import annotations

import argparse
import csv
import math
import random
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=35)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--min-move-dbu", type=int, default=4000)
    parser.add_argument("--max-move-dbu", type=int, default=20000)
    parser.add_argument("--row-x-min-dbu", type=int, default=1380)
    parser.add_argument("--row-x-max-dbu", type=int, default=78660)
    parser.add_argument(
        "--exclude-pool", type=Path, action="append", default=[],
        help="Previously evaluated candidate pool(s); exact instance/target moves are excluded.",
    )
    args = parser.parse_args()

    if args.count < 1 or args.min_move_dbu < 1 or args.max_move_dbu < args.min_move_dbu:
        raise SystemExit("count and move range must be positive, with max >= min")

    with args.inventory.open(newline="") as stream:
        cells = list(csv.DictReader(stream, delimiter="\t"))
    placed = [
        row for row in cells
        if row["status"] in {"PLACED", "FIXED", "COVER", "FIRM", "LOCKED"}
    ]
    flops = [
        row for row in cells
        if row["status"] == "PLACED"
        if row["master"].startswith("sky130_fd_sc_hd__")
        and ("dfxtp" in row["master"] or "edfxtp" in row["master"])
    ]
    if not flops:
        raise SystemExit("inventory contains no placed sequential cells")

    excluded_moves: set[tuple[str, int, int]] = set()
    for path in args.exclude_pool:
        with path.open(newline="") as stream:
            for row in csv.DictReader(stream, delimiter="\t"):
                excluded_moves.add((
                    row["instance"], int(row["target_x_dbu"]), int(row["target_y_dbu"])
                ))

    # Infer the legal x-site pitch from the placed-cell grid and dimensions.
    grid_values = [int(row["x_dbu"]) - args.row_x_min_dbu for row in placed]
    grid_values.extend(int(row["width_dbu"]) for row in placed)
    pitch = 0
    for value in grid_values:
        pitch = math.gcd(pitch, abs(value))
    if pitch <= 0:
        raise SystemExit("could not infer a positive placement-site pitch")

    occupied_by_y: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for row in placed:
        x = int(row["x_dbu"])
        if row in placed:
            occupied_by_y[int(row["y_dbu"])].append((x, x + int(row["width_dbu"])))

    options_by_instance: dict[str, list[int]] = {}
    for flop in flops:
        instance = flop["instance"]
        x = int(flop["x_dbu"])
        y = int(flop["y_dbu"])
        width = int(flop["width_dbu"])
        occupied = occupied_by_y[y]
        options = []
        first_site = args.row_x_min_dbu + ((x - args.row_x_min_dbu) % pitch)
        for target_x in range(first_site, args.row_x_max_dbu - width + 1, pitch):
            displacement = abs(target_x - x)
            if not (args.min_move_dbu <= displacement <= args.max_move_dbu):
                continue
            if (instance, target_x, y) in excluded_moves:
                continue
            if any(target_x < other_hi and target_x + width > other_lo
                   for other_lo, other_hi in occupied if other_lo != x):
                continue
            options.append(target_x)
        if options:
            options_by_instance[instance] = options

    if not options_by_instance:
        raise SystemExit("no legal same-row targets found in the requested local move range")

    rng = random.Random(args.seed)
    instances = list(options_by_instance)
    rng.shuffle(instances)
    selected: list[tuple[dict[str, str], int]] = []
    selected_pairs: set[tuple[str, int]] = set()
    # Prefer one move per flop so the pool samples the full clock-sink set.
    for instance in instances:
        if len(selected) >= args.count:
            break
        flop = next(row for row in flops if row["instance"] == instance)
        choices = options_by_instance[instance][:]
        rng.shuffle(choices)
        target_x = choices[0]
        selected.append((flop, target_x))
        selected_pairs.add((instance, target_x))

    # If the requested pool is larger than the number of movable flops, fill
    # it with additional unique instance/target pairs from the same base ODB.
    if len(selected) < args.count:
        remaining = [
            (row, target_x)
            for row in flops
            for target_x in options_by_instance.get(row["instance"], [])
            if (row["instance"], target_x) not in selected_pairs
        ]
        rng.shuffle(remaining)
        selected.extend(remaining[: args.count - len(selected)])

    if len(selected) < args.count:
        raise SystemExit(
            f"requested {args.count} candidates but found only {len(selected)} unique legal local moves"
        )

    output_rows = []
    for index, (flop, target_x) in enumerate(selected, start=1):
        x = int(flop["x_dbu"])
        y = int(flop["y_dbu"])
        output_rows.append({
            "candidate_id": f"rand{index:03d}",
            "instance": flop["instance"],
            "master": flop["master"],
            "from_x_dbu": x,
            "from_y_dbu": y,
            "target_x_dbu": target_x,
            "target_y_dbu": y,
            "direction": "right" if target_x > x else "left",
            "min_displacement_dbu": args.min_move_dbu,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(output_rows[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(output_rows)
    unique_instances = len({row["instance"] for row in output_rows})
    print(
        f"GCD_RANDOM_RELOCATION_POOL candidates={len(output_rows)} "
        f"unique_flops={unique_instances} site_pitch_dbu={pitch} seed={args.seed} "
        f"move_range_dbu={args.min_move_dbu}:{args.max_move_dbu} "
        f"excluded_moves={len(excluded_moves)} output={args.output}"
    )


if __name__ == "__main__":
    main()
