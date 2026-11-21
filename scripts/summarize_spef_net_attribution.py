#!/usr/bin/env python3
"""Attribute routed SPEF switching-power deltas to per-net activity and capacitance."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
ACTIVITY_RE = re.compile(
    r"(?P<pin>\S+) transitions (?P<transitions>[-+0-9.eE]+) "
    r"activity (?P<density>[-+0-9.eE]+) duty (?P<duty>[-+0-9.eE]+)"
)
CAP_TAG = "#SPEF_DRIVER_CAP\t"
TOTAL_RE = re.compile(
    r"^Total\s+[-+0-9.eE]+\s+(?P<switching>[-+0-9.eE]+)", re.MULTILINE
)
ANNOTATED_RE = re.compile(r"^Annotated\s+(?P<count>\d+) pin activities\.", re.MULTILINE)
UNANNOTATED_RE = re.compile(r"^unannotated\s+(?P<count>\d+)", re.MULTILINE)


def read_activity(log_path: Path) -> tuple[dict[str, float], int, int]:
    activities: dict[str, float] = {}
    text = log_path.read_text(errors="replace")
    for line in text.splitlines():
        match = ACTIVITY_RE.search(line)
        if match:
            activities[match.group("pin")] = float(match.group("density"))
    annotated = ANNOTATED_RE.search(text)
    unannotated = UNANNOTATED_RE.search(text)
    if annotated is None or unannotated is None:
        raise ValueError(f"missing activity annotation counts in {log_path}")
    return activities, int(annotated.group("count")), int(unannotated.group("count"))


def read_net_data(cap_path: Path, activities: dict[str, float]) -> tuple[dict[str, dict[str, float | str]], list[str]]:
    nets: dict[str, dict[str, float | str]] = {}
    pins: dict[str, tuple[str, float, float, float, float]] = {}
    with cap_path.open(errors="replace") as source:
        for line in source:
            if not line.startswith(CAP_TAG):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 9 and fields[1] == "pin":
                continue
            if len(fields) != 9:
                raise ValueError(f"malformed SPEF capacitance row in {cap_path}: {line!r}")
            _, pin, net, pin_cap, wire_cap, total_cap, density, duty, origin = fields
            pin_values = (net, float(pin_cap), float(wire_cap), float(total_cap), float(density))
            if pin in pins:
                previous = pins[pin]
                if previous[1:] != pin_values[1:]:
                    raise ValueError(
                        f"hierarchical aliases have inconsistent driver data for {pin} in {cap_path}"
                    )
                # One leaf output may be visible through multiple hierarchical
                # net aliases. OpenSTA charges its switching power once.
                continue
            pins[pin] = pin_values
            if net in nets:
                raise ValueError(f"multiple output drivers for net {net} in {cap_path}")
            nets[net] = {
                "pin": pin,
                "pin_cap": float(pin_cap),
                "wire_cap": float(wire_cap),
                "total_cap": float(total_cap),
                "density": float(density),
                "duty": float(duty),
                "activity_origin": origin,
                "annotated": float(pin in activities),
            }
    driver_pins = {str(row["pin"]) for row in nets.values()}
    unmatched = sorted(pin for pin in activities if pin not in driver_pins)
    return nets, unmatched


def read_reported_switching(log_path: Path) -> float:
    match = TOTAL_RE.search(log_path.read_text(errors="replace"))
    if match is None:
        raise ValueError(f"missing OpenSTA Total switching power in {log_path}")
    return float(match.group("switching"))


def analyze_workload(
    workload: str,
    baseline_dir: Path,
    candidate_dir: Path,
    output_dir: Path,
    focus_instances: list[str],
) -> dict[str, float | int | str | list[dict[str, float | str]]]:
    base_log = baseline_dir / f"{workload}.log"
    cand_log = candidate_dir / f"{workload}.log"
    base_activity, base_annotated, base_unannotated = read_activity(base_log)
    cand_activity, cand_annotated, cand_unannotated = read_activity(cand_log)
    base_nets, base_unmatched = read_net_data(
        baseline_dir / f"{workload}.driver_caps.tsv", base_activity
    )
    cand_nets, cand_unmatched = read_net_data(
        candidate_dir / f"{workload}.driver_caps.tsv", cand_activity
    )

    factor = 0.5 * VOLTAGE**2
    rows: list[dict[str, float | str]] = []
    for net in sorted(set(base_nets) | set(cand_nets)):
        base = base_nets.get(net, {})
        cand = cand_nets.get(net, {})
        base_pin = str(base.get("pin", ""))
        cand_pin = str(cand.get("pin", ""))
        base_pin_cap = float(base.get("pin_cap", 0.0))
        cand_pin_cap = float(cand.get("pin_cap", 0.0))
        base_wire_cap = float(base.get("wire_cap", 0.0))
        cand_wire_cap = float(cand.get("wire_cap", 0.0))
        base_cap = float(base.get("total_cap", 0.0))
        cand_cap = float(cand.get("total_cap", 0.0))
        base_density = float(base.get("density", 0.0))
        cand_density = float(cand.get("density", 0.0))
        base_power = factor * base_cap * base_density
        cand_power = factor * cand_cap * cand_density
        pin_component = factor * base_density * (cand_pin_cap - base_pin_cap)
        wire_component = factor * base_density * (cand_wire_cap - base_wire_cap)
        activity_component = factor * cand_cap * (cand_density - base_density)
        rows.append(
            {
                "net": net,
                "baseline_driver_pin": base_pin,
                "candidate_driver_pin": cand_pin,
                "baseline_activity_annotated": int(bool(base.get("annotated", 0.0))),
                "candidate_activity_annotated": int(bool(cand.get("annotated", 0.0))),
                "baseline_activity_origin": str(base.get("activity_origin", "missing")),
                "candidate_activity_origin": str(cand.get("activity_origin", "missing")),
                "baseline_density_hz": base_density,
                "candidate_density_hz": cand_density,
                "delta_density_hz": cand_density - base_density,
                "baseline_pin_cap_f": base_pin_cap,
                "candidate_pin_cap_f": cand_pin_cap,
                "delta_pin_cap_f": cand_pin_cap - base_pin_cap,
                "baseline_wire_cap_f": base_wire_cap,
                "candidate_wire_cap_f": cand_wire_cap,
                "delta_wire_cap_f": cand_wire_cap - base_wire_cap,
                "baseline_load_cap_f": base_cap,
                "candidate_load_cap_f": cand_cap,
                "delta_load_cap_f": cand_cap - base_cap,
                "baseline_switching_power_w": base_power,
                "candidate_switching_power_w": cand_power,
                "delta_switching_power_w": cand_power - base_power,
                "pin_cap_contribution_w": pin_component,
                "wire_cap_contribution_w": wire_component,
                "activity_contribution_w": activity_component,
            }
        )

    rows.sort(key=lambda row: abs(float(row["delta_switching_power_w"])), reverse=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    net_path = output_dir / f"{workload}.per_net_delta.csv"
    with net_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    base_reported = read_reported_switching(base_log)
    cand_reported = read_reported_switching(cand_log)
    base_scored = sum(float(row["baseline_switching_power_w"]) for row in rows)
    cand_scored = sum(float(row["candidate_switching_power_w"]) for row in rows)
    net_delta = cand_scored - base_scored
    report_delta = cand_reported - base_reported
    residual = net_delta - report_delta
    relative_error = abs(residual) / max(abs(report_delta), 1.0e-12)
    focus_instance_delta = {}
    for instance in focus_instances:
        focus_rows = [
            row for row in rows
            if str(row["baseline_driver_pin"]).startswith(f"{instance}/")
            or str(row["candidate_driver_pin"]).startswith(f"{instance}/")
        ]
        focus_instance_delta[instance] = sum(
            float(row["delta_switching_power_w"]) for row in focus_rows
        )

    return {
        "workload": workload,
        "baseline_annotated": base_annotated,
        "candidate_annotated": cand_annotated,
        "baseline_unannotated": base_unannotated,
        "candidate_unannotated": cand_unannotated,
        "baseline_driver_nets": len(base_nets),
        "candidate_driver_nets": len(cand_nets),
        "baseline_activity_without_driver_net": len(base_unmatched),
        "candidate_activity_without_driver_net": len(cand_unmatched),
        "baseline_reported_switching_w": base_reported,
        "baseline_per_net_switching_w": base_scored,
        "candidate_reported_switching_w": cand_reported,
        "candidate_per_net_switching_w": cand_scored,
        "reported_delta_w": report_delta,
        "per_net_delta_w": net_delta,
        "delta_residual_w": residual,
        "delta_relative_error": relative_error,
        "pin_cap_delta_contribution_w": sum(float(row["pin_cap_contribution_w"]) for row in rows),
        "wire_cap_delta_contribution_w": sum(float(row["wire_cap_contribution_w"]) for row in rows),
        "activity_delta_contribution_w": sum(float(row["activity_contribution_w"]) for row in rows),
        "focus_instance_output_net_delta_w": focus_instance_delta,
        "clock_net_delta_w": sum(
            float(row["delta_switching_power_w"]) for row in rows if str(row["net"]).startswith("clknet_")
        ),
        "nonclock_net_delta_w": sum(
            float(row["delta_switching_power_w"]) for row in rows if not str(row["net"]).startswith("clknet_")
        ),
        "baseline_total_relative_error": abs(base_scored - base_reported) / base_reported if base_reported else 0.0,
        "candidate_total_relative_error": abs(cand_scored - cand_reported) / cand_reported if cand_reported else 0.0,
        "per_net_csv": str(net_path),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--max-relative-error", type=float, default=0.01)
    parser.add_argument(
        "--focus-instance",
        action="append",
        default=None,
        help="instance whose output-net delta should be highlighted; repeat as needed (default: place538)",
    )
    args = parser.parse_args()
    focus_instances = args.focus_instance or ["place538"]

    try:
        results = [
            analyze_workload(
                workload, args.baseline_dir, args.candidate_dir, args.output_dir,
                focus_instances,
            )
            for workload in WORKLOADS
        ]
    except (OSError, ValueError) as exc:
        print(f"SPEF net attribution failed: {exc}", file=sys.stderr)
        return 2

    lines = [
        "Per-net routed SPEF switching-power attribution",
        "Each net is scored once from its output driver using 0.5 x load capacitance x V^2 x OpenSTA pin activity at 1.8 V.",
        "Delta components use baseline density for capacitance changes and candidate capacitance for activity changes.",
        f"Reconciliation limit: each per-net whole-design total must agree with OpenSTA within {args.max_relative_error:.2%}.",
        "",
    ]
    failing = []
    for result in results:
        workload = str(result["workload"])
        error = float(result["delta_relative_error"])
        base_total_error = float(result["baseline_total_relative_error"])
        cand_total_error = float(result["candidate_total_relative_error"])
        if max(base_total_error, cand_total_error) > args.max_relative_error:
            failing.append(workload)
        lines.extend(
            [
                f"{workload}:",
                f"  OpenSTA switching: {float(result['baseline_reported_switching_w']):.12e} -> "
                f"{float(result['candidate_reported_switching_w']):.12e} W "
                f"(delta {float(result['reported_delta_w']):+.12e} W).",
                f"  Per-net sum: {float(result['baseline_per_net_switching_w']):.12e} -> "
                f"{float(result['candidate_per_net_switching_w']):.12e} W "
                f"(delta {float(result['per_net_delta_w']):+.12e} W; delta residual "
                f"{float(result['delta_residual_w']):+.3e} W, {error:.3%} of the net delta).",
                f"  Whole-total reconciliation: baseline {base_total_error:.3e}; "
                f"candidate {cand_total_error:.3e} relative error.",
                f"  Delta attribution: pin capacitance {float(result['pin_cap_delta_contribution_w']):+.3e} W; "
                f"wire capacitance {float(result['wire_cap_delta_contribution_w']):+.3e} W; "
                f"activity {float(result['activity_delta_contribution_w']):+.3e} W.",
                f"  Net classes: clock {float(result['clock_net_delta_w']):+.3e} W; "
                f"non-clock {float(result['nonclock_net_delta_w']):+.3e} W.",
                f"  VCD annotated pins: {result['baseline_annotated']} baseline / {result['candidate_annotated']} candidate; "
                f"unannotated: {result['baseline_unannotated']} / {result['candidate_unannotated']}; "
                f"driver nets: {result['baseline_driver_nets']} / {result['candidate_driver_nets']}.",
                f"  Per-net table: {result['per_net_csv']}",
            ]
        )
        focus_values = result["focus_instance_output_net_delta_w"]
        assert isinstance(focus_values, dict)
        for instance, power_delta in focus_values.items():
            lines.append(
                f"  Focused output-net delta ({instance}): {float(power_delta):+.3e} W."
            )
        rows = result["rows"]
        assert isinstance(rows, list)
        positive = sorted(
            (row for row in rows if float(row["delta_switching_power_w"]) > 0.0),
            key=lambda row: float(row["delta_switching_power_w"]),
            reverse=True,
        )[:5]
        negative = sorted(
            (row for row in rows if float(row["delta_switching_power_w"]) < 0.0),
            key=lambda row: float(row["delta_switching_power_w"]),
        )[:5]
        lines.append("  Largest positive net deltas:")
        if positive:
            for row in positive:
                lines.append(
                    f"    {row['net']}: {float(row['delta_switching_power_w']):+.3e} W; "
                    f"wire cap {float(row['delta_wire_cap_f']):+.3e} F; "
                    f"density {float(row['baseline_density_hz']):.3g}->{float(row['candidate_density_hz']):.3g} Hz"
                )
        else:
            lines.append("    none")
        lines.append("  Largest negative net deltas:")
        if negative:
            for row in negative:
                lines.append(
                    f"    {row['net']}: {float(row['delta_switching_power_w']):+.3e} W; "
                    f"wire cap {float(row['delta_wire_cap_f']):+.3e} F; "
                    f"density {float(row['baseline_density_hz']):.3g}->{float(row['candidate_density_hz']):.3g} Hz"
                )
        else:
            lines.append("    none")
        lines.append("")

    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text("\n".join(lines) + "\n")
    print(args.summary.read_text(), end="")
    if failing:
        print(
            f"per-net whole-design total failed {args.max_relative_error:.2%} reconciliation for: "
            + ", ".join(failing),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
