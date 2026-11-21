#!/usr/bin/env python3
"""Generate a frozen, branch-targeted GCD one-flop relocation pool."""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from pathlib import Path


BRANCH = "clknet_2_3__leaf_clk"
PRIMARY_SINK = r"dpath.a_reg.out\[8\]$_DFFE_PP_"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--branch-sinks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exclude-pool", type=Path, action="append", default=[])
    parser.add_argument("--count", type=int, default=32)
    parser.add_argument("--max-move-dbu", type=int, default=60000)
    parser.add_argument("--row-x-min-dbu", type=int, default=1380)
    parser.add_argument("--row-x-max-dbu", type=int, default=78660)
    args = parser.parse_args()

    inventory = read_tsv(args.inventory)
    placed = [row for row in inventory if row["status"] in {"PLACED", "FIXED", "COVER", "FIRM", "LOCKED"}]
    by_name = {row["instance"]: row for row in inventory}
    if not placed:
        raise SystemExit("placement inventory has no placed cells")

    pitch = 0
    for row in placed:
        pitch = math.gcd(pitch, abs(int(row["x_dbu"]) - args.row_x_min_dbu))
        pitch = math.gcd(pitch, int(row["width_dbu"]))
    if pitch <= 0:
        raise SystemExit("could not infer a positive placement site pitch")

    excluded: set[tuple[str, int, int]] = set()
    for path in args.exclude_pool:
        for row in read_tsv(path):
            excluded.add((row["instance"], int(row["target_x_dbu"]), int(row["target_y_dbu"])))

    branch_rows = [
        row for row in read_tsv(args.branch_sinks)
        if row.get("branch") == BRANCH
    ]
    sinks = list(dict.fromkeys(row["instance"] for row in branch_rows))
    missing = [name for name in sinks if name not in by_name]
    if missing:
        raise SystemExit(f"branch sink(s) missing from placement inventory: {missing}")
    if PRIMARY_SINK not in sinks:
        raise SystemExit(f"primary threshold-probe sink {PRIMARY_SINK} is not in {BRANCH}")

    occupied_by_y: dict[int, list[tuple[int, int, str]]] = defaultdict(list)
    for row in placed:
        x, y = int(row["x_dbu"]), int(row["y_dbu"])
        occupied_by_y[y].append((x, x + int(row["width_dbu"]), row["instance"]))

    proposals: list[dict[str, str]] = []
    seen: set[tuple[str, int, int]] = set()

    def add_sink_moves(instance: str, desired_offsets: list[int], target_count: int) -> None:
        cell = by_name[instance]
        if cell["status"] != "PLACED" or not any(k in cell["master"] for k in ("dfxtp", "edfxtp")):
            return
        x, y, width = int(cell["x_dbu"]), int(cell["y_dbu"]), int(cell["width_dbu"])
        row_occupancy = occupied_by_y[y]
        candidates: list[tuple[int, int]] = []
        for offset in desired_offsets:
            if offset == 0 or abs(offset) < 4000 or abs(offset) > args.max_move_dbu:
                continue
            target_x = x + offset
            if (target_x - args.row_x_min_dbu) % pitch:
                continue
            if target_x < args.row_x_min_dbu or target_x + width > args.row_x_max_dbu:
                continue
            if any(
                other_name != instance and target_x < other_hi and target_x + width > other_lo
                for other_lo, other_hi, other_name in row_occupancy
            ):
                continue
            key = (instance, target_x, y)
            if key in excluded or key in seen:
                continue
            candidates.append((target_x, offset))

        # Fill gaps caused by old candidates or occupied sites with nearby legal
        # sites, keeping the sweep local and deterministic.
        max_radius = args.max_move_dbu // pitch
        for radius in range(10, max_radius + 1):
            if len(candidates) >= target_count:
                break
            for sign in (1, -1):
                offset = sign * radius * pitch
                target_x = x + offset
                if not (4000 <= abs(offset) <= args.max_move_dbu):
                    continue
                if target_x < args.row_x_min_dbu or target_x + width > args.row_x_max_dbu:
                    continue
                if any(
                    other_name != instance and target_x < other_hi and target_x + width > other_lo
                    for other_lo, other_hi, other_name in row_occupancy
                ):
                    continue
                key = (instance, target_x, y)
                if key in excluded or key in seen or any(old_x == target_x for old_x, _ in candidates):
                    continue
                candidates.append((target_x, offset))
                if len(candidates) >= target_count:
                    break

        # Preserve desired sweep order, then use the deterministic legal fillers.
        selected = candidates[:target_count]
        for target_x, offset in selected:
            seen.add((instance, target_x, y))
            proposals.append({
                "candidate_id": f"br3_{len(proposals) + 1:03d}",
                "instance": instance,
                "master": cell["master"],
                "from_x_dbu": str(x),
                "from_y_dbu": str(y),
                "target_x_dbu": str(target_x),
                "target_y_dbu": str(y),
                "direction": "right" if offset > 0 else "left",
                "min_displacement_dbu": "4000",
                "branch": BRANCH,
                "target_offset_dbu": str(offset),
            })

    # Dense horizontal sweep around the previously observed +6.9 um event.
    # The exact target and prior pool points are excluded, preserving an
    # independent local boundary sample.
    primary_offsets = [
        5980, 6440, 7360, 8280, 5060, 8740,
        -4600, -5980, -6900, -7820, -9200, -11040,
    ]
    add_sink_moves(PRIMARY_SINK, primary_offsets, 22)

    # Probe each other sink in the same CTS leaf using symmetric local moves.
    # These test whether the boundary is specific to the known sink or to the
    # whole branch's spatial arrangement.
    offsets = [4600, -4600, 9200, -9200]
    other_counts = {
        r"dpath.a_reg.out\[9\]$_DFFE_PP_": 4,
        r"dpath.b_reg.out\[13\]$_DFFE_PP_": 16,
    }
    for instance in sinks:
        if instance == PRIMARY_SINK:
            continue
        add_sink_moves(instance, offsets, other_counts.get(instance, 0))

    if len(proposals) < args.count:
        raise SystemExit(
            f"only generated {len(proposals)} legal branch-targeted moves; requested {args.count}"
        )
    proposals = proposals[:args.count]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(proposals[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(proposals)
    print(
        f"GCD_BRANCH_TARGETED_POOL candidates={len(proposals)} branch={BRANCH} "
        f"primary={PRIMARY_SINK} site_pitch_dbu={pitch} excluded_moves={len(excluded)} "
        f"output={args.output}"
    )


if __name__ == "__main__":
    main()
