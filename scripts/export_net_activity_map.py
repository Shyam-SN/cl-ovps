#!/usr/bin/env python3
"""Write one activity-density row per driven net from the scored cost table."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    density_by_net: dict[str, float] = {}
    with args.input.open(newline="") as source:
        for row in csv.DictReader(source):
            net = row["net"]
            density = float(row["density_hz"])
            if net in density_by_net and abs(density_by_net[net] - density) > 1e-6:
                raise ValueError(f"multiple drivers with inconsistent activity for net {net}")
            density_by_net[net] = density
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow(("net", "density_hz"))
        for net, density in sorted(density_by_net.items()):
            writer.writerow((net, f"{density:.12g}"))
    print(f"Wrote {len(density_by_net)} net activity rows to {args.output}")


if __name__ == "__main__":
    main()
