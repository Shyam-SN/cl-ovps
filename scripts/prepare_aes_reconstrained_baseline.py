#!/usr/bin/env python3
"""Clone an existing AES placed checkpoint and retarget its SDC to a new clock period."""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "build/orfs/results/sky130hd/aes"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tag", required=True)
    parser.add_argument("--new-tag", required=True)
    parser.add_argument("--period-ns", type=float, default=3.8)
    parser.add_argument("--io-fraction", type=float, default=0.2)
    args = parser.parse_args()

    source = RESULTS / args.source_tag
    destination = RESULTS / args.new_tag
    if destination.exists():
        raise FileExistsError(destination)
    if not (source / "3_5_place_dp.odb").is_file() or not (source / "3_place.sdc").is_file():
        raise FileNotFoundError(f"source placement checkpoint is incomplete: {source}")
    destination.mkdir(parents=True)
    shutil.copy2(source / "3_5_place_dp.odb", destination / "3_5_place_dp.odb")
    shutil.copy2(source / "3_place.odb", destination / "3_place.odb")

    sdc = (source / "3_place.sdc").read_text()
    clocks = re.sub(r"(create_clock -name (?:clk|vclk_clk) -period )([0-9.]+)",
                    lambda match: match.group(1) + f"{args.period_ns:.4f}", sdc)
    io_delay = f"{args.period_ns * args.io_fraction:.4f}"
    delays, count = re.subn(r"(set_(?:input|output)_delay )[0-9.]+",
                            lambda match: match.group(1) + io_delay, clocks)
    if clocks == sdc or count == 0 or "3.6000" in delays or "0.7200" in delays:
        raise ValueError("failed to retarget all AES clocks and I/O delays in the generated SDC")
    (destination / "3_place.sdc").write_text(delays)
    print(f"AES_RECONSTRAINED_BASELINE source={args.source_tag} variant={args.new_tag} "
          f"period_ns={args.period_ns:.4f} io_delay_ns={io_delay} io_delay_entries={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
