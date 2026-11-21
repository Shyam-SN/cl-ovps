#!/usr/bin/env python3
"""Summarize matched GCD placement and routed-SPEF workload reports."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def read_metrics(report_dir: Path, workload: str) -> dict[str, float | int]:
    placement = (report_dir / f"{workload}.placement.log").read_text()
    routed = (report_dir / f"{workload}.routed.log").read_text()
    total_match = re.search(
        rf"^Total\s+({FLOAT})\s+({FLOAT})\s+({FLOAT})\s+({FLOAT})",
        placement,
        re.MULTILINE,
    )
    routed_match = re.search(
        rf"SPEF_METRICS workload={re.escape(workload)} switching_w=({FLOAT}) "
        rf"setup_ns=({FLOAT}) hold_ns=({FLOAT})",
        routed,
    )
    annotated_match = re.search(r"Annotated\s+(\d+) pin activities\.", routed)
    unannotated_match = re.search(r"^unannotated\s+(\d+)$", routed, re.MULTILINE)
    if not all((total_match, routed_match, annotated_match, unannotated_match)):
        raise ValueError(f"missing or malformed power metrics for workload {workload}")
    if int(unannotated_match.group(1)) != 0:
        raise ValueError(f"VCD has unannotated pins for workload {workload}")
    if re.search(r"clock .* vcd period .* differs from SDC clock period", routed):
        raise ValueError(f"VCD and SDC clock periods differ for workload {workload}")
    return {
        "placement_switch_w": float(total_match.group(2)),
        "placement_total_w": float(total_match.group(4)),
        "routed_switch_w": float(routed_match.group(1)),
        "setup_ns": float(routed_match.group(2)),
        "hold_ns": float(routed_match.group(3)),
        "annotated_pins": int(annotated_match.group(1)),
        "unannotated_pins": int(unannotated_match.group(1)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = {workload: read_metrics(args.report_dir, workload) for workload in ("idle", "typical", "peak")}
    integrity = (args.report_dir / "integrity.tsv").read_text().splitlines()
    if len(integrity) != 6 or any(not line.endswith("\ttrue") for line in integrity[1:]):
        raise ValueError("source artifact integrity checks did not all pass")
    if any(row["setup_ns"] < 0.05 or row["hold_ns"] < 0.05 for row in rows.values()):
        raise ValueError("routed baseline does not meet the 50 ps setup/hold guard")

    lines = [
        "# GCD baseline power reference",
        "",
        "Second-design baseline for the 20 ns SKY130HD setup. Power values are switching power; placement values are pre-CTS and routed values include the clock network.",
        "",
        "| Workload | Placement switching (µW) | Routed SPEF switching (µW) | Setup slack (ns) | Hold slack (ns) | Annotated pins |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for workload, row in rows.items():
        lines.append(
            f"| {workload} | {row['placement_switch_w'] * 1e6:.3f} "
            f"| {row['routed_switch_w'] * 1e6:.3f} | {row['setup_ns']:.3f} "
            f"| {row['hold_ns']:.3f} | {row['annotated_pins']} |"
        )
    lines.extend(
        [
            "",
            "Checks: every workload has zero unannotated VCD pins, the VCD and SDC clock periods match, routed setup/hold slack exceed 50 ps, and placement/routed source hashes remain unchanged during read-only power reporting.",
            "",
            "This is a baseline only; it does not yet show that the candidate-held-out predictor generalizes across designs.",
            "",
        ]
    )
    args.output.write_text("\n".join(lines))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
