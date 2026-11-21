#!/usr/bin/env python3
"""Join OpenSTA VCD activity with placement-estimated driver load capacitance."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


ACTIVITY_RE = re.compile(
    r"(?P<pin>\S+) transitions (?P<transitions>[-+0-9.eE]+) "
    r"activity (?P<density>[-+0-9.eE]+) duty (?P<duty>[-+0-9.eE]+)"
)
TOTAL_RE = re.compile(
    r"^Total\s+[-+0-9.eE]+\s+(?P<switching>[-+0-9.eE]+)", re.MULTILINE
)


def read_activities(log_path: Path) -> dict[str, tuple[float, float, float]]:
    activities: dict[str, tuple[float, float, float]] = {}
    with log_path.open(errors="replace") as log:
        for line in log:
            match = ACTIVITY_RE.search(line)
            if match:
                pin = match.group("pin")
                activities[pin] = (
                    float(match.group("transitions")),
                    float(match.group("density")),
                    float(match.group("duty")),
                )
    return activities


def workload_log(report_dir: Path, workload: str) -> Path:
    for candidate in (report_dir / f"{workload}.log", report_dir / f"{workload}.placement.log"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"no power log found for workload {workload} in {report_dir}")


def read_caps(cap_path: Path) -> dict[str, tuple[str, float, float, float]]:
    caps: dict[str, tuple[str, float, float, float]] = {}
    with cap_path.open(errors="replace") as cap_file:
        for line in cap_file:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 6 and fields[1] == "pin":
                continue
            if len(fields) != 6:
                raise ValueError(f"malformed capacitance row in {cap_path}: {line!r}")
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            caps[pin] = (net, float(pin_cap), float(wire_cap), float(total_cap))
    return caps


def read_reported_switching(log_path: Path) -> float:
    text = log_path.read_text(errors="replace")
    match = TOTAL_RE.search(text)
    if not match:
        raise ValueError(f"could not find OpenSTA Total switching power in {log_path}")
    return float(match.group("switching"))


def summarize(report_dir: Path, workload: str) -> dict[str, float | int | str]:
    log_path = workload_log(report_dir, workload)
    activities = read_activities(log_path)
    caps = read_caps(report_dir / f"{workload}.driver_caps.tsv")

    rows = []
    unmatched = []
    for pin, (transitions, density, duty) in activities.items():
        cap_row = caps.get(pin)
        if cap_row is None:
            unmatched.append(pin)
            continue
        net, pin_cap, wire_cap, total_cap = cap_row
        # Sky130 HD typical is nominal 1.8 V. OpenSTA's switching formula is
        # 0.5 * C_load * V^2 * transition_density.
        estimated_power = 0.5 * total_cap * (1.8**2) * density
        rows.append(
            {
                "pin": pin,
                "net": net,
                "transitions": transitions,
                "density_hz": density,
                "duty": duty,
                "pin_cap_f": pin_cap,
                "wire_cap_f": wire_cap,
                "load_cap_f": total_cap,
                "estimated_switching_power_w": estimated_power,
            }
        )

    rows.sort(key=lambda row: row["estimated_switching_power_w"], reverse=True)
    per_net_path = report_dir / f"{workload}.per_net_cost.csv"
    with per_net_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    reported_power = read_reported_switching(log_path)
    estimated_power = sum(row["estimated_switching_power_w"] for row in rows)
    return {
        "workload": workload,
        "vcd_annotated_pins": len(activities),
        "matched_output_driver_pins": len(rows),
        "activity_pins_without_output_driver_cost": len(unmatched),
        "reported_switching_power_w": reported_power,
        "scored_switching_power_w": estimated_power,
        "score_to_report_ratio": estimated_power / reported_power if reported_power else 0.0,
        "non_driver_or_unmatched_pin_examples": ", ".join(unmatched[:5]),
        "per_net_csv": str(per_net_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--workloads", nargs="+", required=True)
    parser.add_argument(
        "--max-relative-error",
        type=float,
        default=0.01,
        help="fail if the aggregate score differs from OpenSTA by more than this fraction (default: 1%%)",
    )
    args = parser.parse_args()

    summaries = [summarize(args.report_dir, workload) for workload in args.workloads]
    summary_path = args.report_dir / "activity_cost_summary.csv"
    with summary_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(summaries[0]))
        writer.writeheader()
        writer.writerows(summaries)

    print(f"Per-net activity-weighted costs: {args.report_dir}/*.per_net_cost.csv")
    print(f"Aggregate reconciliation: {summary_path}")
    for summary in summaries:
        print(
            f"{summary['workload']}: scored "
            f"{summary['matched_output_driver_pins']} output-driver pins from "
            f"{summary['vcd_annotated_pins']} VCD-annotated pins; "
            f"scored/reported switching power = "
            f"{summary['score_to_report_ratio']:.4f}"
        )
        if summary["non_driver_or_unmatched_pin_examples"]:
            print(
                "  activity pins without a scored driver load (first five): "
                f"{summary['non_driver_or_unmatched_pin_examples']}"
            )
    failed = [
        summary
        for summary in summaries
        if abs(1.0 - float(summary["score_to_report_ratio"])) > args.max_relative_error
    ]
    if failed:
        workloads = ", ".join(str(summary["workload"]) for summary in failed)
        print(
            f"aggregate activity-cost score exceeded the {args.max_relative_error:.2%} "
            f"reconciliation tolerance for: {workloads}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"activity-cost scoring failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
