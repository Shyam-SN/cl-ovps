#!/usr/bin/env python3
"""Choose a deterministic, spatially spread set of GCD sequential cells to relocate."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--count", type=int, default=16)
    args = parser.parse_args()

    with args.inventory.open(newline="") as stream:
        cells = list(csv.DictReader(stream, delimiter="\t"))
    flops = [
        row for row in cells
        if row["status"] == "PLACED"
        and row["master"].startswith("sky130_fd_sc_hd__")
        and ("dfxtp" in row["master"] or "edfxtp" in row["master"])
    ]
    if args.count < 1 or args.count > len(flops):
        raise SystemExit(f"count must be between 1 and {len(flops)}")

    # Sort by physical row and x coordinate, then sample evenly across the die.
    flops.sort(key=lambda row: (int(row["y_dbu"]), int(row["x_dbu"]), row["instance"]))
    if args.count == 1:
        chosen = [flops[len(flops) // 2]]
    else:
        indices = [round(i * (len(flops) - 1) / (args.count - 1)) for i in range(args.count)]
        chosen = [flops[index] for index in indices]

    x_min, x_max = 1380, 78660
    min_move_dbu = 8000
    rows = []
    for index, cell in enumerate(chosen, start=1):
        x = int(cell["x_dbu"])
        width = int(cell["width_dbu"])
        left_room = x - x_min
        right_room = x_max - (x + width)
        # Prefer an outward move from the die center when enough legal search
        # range exists; otherwise move toward the side with more room.
        center = (x_min + x_max) // 2
        preferred = "left" if x < center else "right"
        room = {"left": left_room, "right": right_room}
        if room[preferred] < min_move_dbu:
            preferred = "left" if left_room >= right_room else "right"
        rows.append({
            "candidate_id": f"reloc{index:02d}",
            "instance": cell["instance"],
            "master": cell["master"],
            "from_x_dbu": x,
            "from_y_dbu": int(cell["y_dbu"]),
            "direction": preferred,
            "min_displacement_dbu": min_move_dbu,
        })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"GCD_RELOCATION_POOL candidates={len(rows)} sequential_cells={len(flops)} output={args.output}")


if __name__ == "__main__":
    main()
