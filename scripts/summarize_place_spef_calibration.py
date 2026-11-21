#!/usr/bin/env python3
"""Compare placement-estimated per-net power changes with routed SPEF changes."""

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
TOTAL_RE = re.compile(
    r"^Total\s+[-+0-9.eE]+\s+(?P<switching>[-+0-9.eE]+)", re.MULTILINE
)
ANNOTATED_RE = re.compile(r"^Annotated\s+(?P<count>\d+) pin activities\.", re.MULTILINE)
UNANNOTATED_RE = re.compile(r"^unannotated\s+(?P<count>\d+)", re.MULTILINE)


def read_place_data(directory: Path, workload: str) -> tuple[dict[str, dict[str, float | str]], float, int, int]:
    log_path = directory / f"{workload}.log"
    cap_path = directory / f"{workload}.driver_caps.tsv"
    text = log_path.read_text(errors="replace")
    activities = {
        match.group("pin"): float(match.group("density"))
        for line in text.splitlines()
        if (match := ACTIVITY_RE.search(line))
    }
    total_match = TOTAL_RE.search(text)
    annotated_match = ANNOTATED_RE.search(text)
    unannotated_match = UNANNOTATED_RE.search(text)
    if not (total_match and annotated_match and unannotated_match):
        raise ValueError(f"missing OpenSTA total or VCD annotation counts in {log_path}")

    nets: dict[str, dict[str, float | str]] = {}
    with cap_path.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 6 and fields[1] == "pin":
                continue
            if len(fields) != 6:
                raise ValueError(f"malformed placement capacitance row in {cap_path}: {line!r}")
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            if net in nets:
                raise ValueError(f"multiple output drivers for net {net} in {cap_path}")
            nets[net] = {
                "pin": pin,
                "pin_cap": float(pin_cap),
                "wire_cap": float(wire_cap),
                "total_cap": float(total_cap),
                "density": activities.get(pin, 0.0),
                "annotated": float(pin in activities),
            }
    return (
        nets,
        float(total_match.group("switching")),
        int(annotated_match.group("count")),
        int(unannotated_match.group("count")),
    )


def read_routed_data(directory: Path, workload: str) -> tuple[dict[str, dict[str, str]], float]:
    rows: dict[str, dict[str, str]] = {}
    with (directory / f"{workload}.per_net_delta.csv").open(newline="") as source:
        for row in csv.DictReader(source):
            rows[row["net"]] = row
    text = (directory / "baseline" / f"{workload}.log").read_text(errors="replace")
    candidate_text = (directory / "candidate" / f"{workload}.log").read_text(errors="replace")
    base_match = TOTAL_RE.search(text)
    candidate_match = TOTAL_RE.search(candidate_text)
    if not (base_match and candidate_match):
        raise ValueError(f"missing routed OpenSTA total for {workload}")
    delta = float(candidate_match.group("switching")) - float(base_match.group("switching"))
    return rows, delta


def read_moved_instance(move_summary: Path, move_eval_table: Path) -> tuple[str, str]:
    with move_summary.open(newline="") as source:
        move = next(csv.DictReader(source, delimiter="\t"))
    with move_eval_table.open(newline="") as source:
        for row in csv.DictReader(source, delimiter="\t"):
            if all(
                row.get(field) == move.get(field)
                for field in ("instance", "from_x", "from_y", "to_x", "to_y")
            ):
                return move["instance"], row.get("seed_net", "")
    raise ValueError("selected placement move does not match a per-workload evaluator row")


def read_and_check_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2:
            raise ValueError(f"malformed integrity manifest row: {line!r}")
        values[fields[0]] = fields[1]
    pairs = (
        ("baseline_place_odb_sha256", "baseline_place_odb_sha256_after"),
        ("candidate_place_odb_sha256", "candidate_place_odb_sha256_after"),
        ("placement_sdc_sha256", "placement_sdc_sha256_after"),
        ("routed_baseline_spef_sha256_before", "routed_baseline_spef_sha256_after"),
        ("routed_candidate_spef_sha256_before", "routed_candidate_spef_sha256_after"),
    )
    for before, after in pairs:
        if not values.get(before) or values.get(before) != values.get(after):
            raise ValueError(f"input integrity check failed for {before}")
    return values


def analyze_workload(
    workload: str,
    place_baseline_dir: Path,
    place_candidate_dir: Path,
    routed_dir: Path,
    output_dir: Path,
    moved_instance: str,
    moved_seed_net: str,
) -> dict[str, float | int | str]:
    baseline, base_reported, base_annotated, base_unannotated = read_place_data(
        place_baseline_dir, workload
    )
    candidate, candidate_reported, candidate_annotated, candidate_unannotated = read_place_data(
        place_candidate_dir, workload
    )
    routed, routed_reported_delta = read_routed_data(routed_dir, workload)
    factor = 0.5 * VOLTAGE**2
    rows: list[dict[str, float | int | str]] = []
    for net in sorted(set(baseline) | set(candidate) | set(routed)):
        base = baseline.get(net, {})
        cand = candidate.get(net, {})
        route = routed.get(net, {})
        base_cap = float(base.get("total_cap", 0.0))
        candidate_cap = float(cand.get("total_cap", 0.0))
        base_pin_cap = float(base.get("pin_cap", 0.0))
        candidate_pin_cap = float(cand.get("pin_cap", 0.0))
        base_wire_cap = float(base.get("wire_cap", 0.0))
        candidate_wire_cap = float(cand.get("wire_cap", 0.0))
        base_density = float(base.get("density", 0.0))
        candidate_density = float(cand.get("density", 0.0))
        placement_power_delta = factor * (
            candidate_cap * candidate_density - base_cap * base_density
        )
        placement_cap_component = factor * base_density * (candidate_cap - base_cap)
        placement_activity_component = factor * candidate_cap * (candidate_density - base_density)
        routed_delta = float(route.get("delta_switching_power_w", 0.0))
        routed_origin = route.get("baseline_activity_origin", "missing")
        is_clock = routed_origin == "clock" or net.startswith("clknet_")
        both_stages = bool(base) and bool(cand) and bool(route)
        common_signal = both_stages and not is_clock
        prediction_error = placement_power_delta - routed_delta
        rows.append(
            {
                "net": net,
                "moved_instance": moved_instance if net == moved_seed_net else "",
                "moved_seed_net": int(net == moved_seed_net),
                "routed_activity_origin": routed_origin,
                "routed_class": "postcts_clock" if is_clock else (
                    "common_signal" if common_signal else "not_common_to_placement"
                ),
                "in_baseline_placement": int(bool(base)),
                "in_candidate_placement": int(bool(cand)),
                "in_routed_spef": int(bool(route)),
                "baseline_place_driver_pin": str(base.get("pin", "")),
                "candidate_place_driver_pin": str(cand.get("pin", "")),
                "baseline_place_density_hz": base_density,
                "candidate_place_density_hz": candidate_density,
                "baseline_place_pin_cap_f": base_pin_cap,
                "candidate_place_pin_cap_f": candidate_pin_cap,
                "delta_place_pin_cap_f": candidate_pin_cap - base_pin_cap,
                "baseline_place_wire_cap_f": base_wire_cap,
                "candidate_place_wire_cap_f": candidate_wire_cap,
                "delta_place_wire_cap_f": candidate_wire_cap - base_wire_cap,
                "baseline_place_load_cap_f": base_cap,
                "candidate_place_load_cap_f": candidate_cap,
                "delta_place_load_cap_f": candidate_cap - base_cap,
                "place_capacitance_power_delta_w": placement_cap_component,
                "place_activity_power_delta_w": placement_activity_component,
                "place_predicted_power_delta_w": placement_power_delta,
                "routed_delta_wire_cap_f": float(route.get("delta_wire_cap_f", 0.0)),
                "routed_delta_load_cap_f": float(route.get("delta_load_cap_f", 0.0)),
                "routed_activity_power_delta_w": float(route.get("activity_contribution_w", 0.0)),
                "routed_power_delta_w": routed_delta,
                "place_minus_routed_error_w": prediction_error,
                "same_power_delta_sign": int(
                    placement_power_delta * routed_delta > 0.0
                    or (placement_power_delta == 0.0 and routed_delta == 0.0)
                ),
            }
        )

    rows.sort(key=lambda row: abs(float(row["place_minus_routed_error_w"])), reverse=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    per_net_path = output_dir / f"{workload}.placement_vs_routed.per_net.csv"
    with per_net_path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)

    place_net_baseline = sum(
        factor * float(net["total_cap"]) * float(net["density"]) for net in baseline.values()
    )
    place_net_candidate = sum(
        factor * float(net["total_cap"]) * float(net["density"]) for net in candidate.values()
    )
    place_net_delta = place_net_candidate - place_net_baseline
    place_report_delta = candidate_reported - base_reported
    common_rows = [row for row in rows if row["routed_class"] == "common_signal"]
    common_route_delta = sum(float(row["routed_power_delta_w"]) for row in common_rows)
    common_place_delta = sum(float(row["place_predicted_power_delta_w"]) for row in common_rows)
    numeric_epsilon = 1.0e-15
    nonzero_place = [
        row for row in common_rows
        if abs(float(row["place_predicted_power_delta_w"])) > numeric_epsilon
    ]
    nonzero_route = [
        row for row in common_rows
        if abs(float(row["routed_power_delta_w"])) > numeric_epsilon
    ]
    sign_matches = sum(
        float(row["place_predicted_power_delta_w"]) * float(row["routed_power_delta_w"]) > 0.0
        for row in nonzero_place
    )
    sign_reversals = sum(
        float(row["place_predicted_power_delta_w"]) * float(row["routed_power_delta_w"]) < 0.0
        for row in nonzero_place
    )
    route_change_place_zero = sum(
        abs(float(row["place_predicted_power_delta_w"])) <= numeric_epsilon
        for row in nonzero_route
    )
    predicted_net_route_delta = sum(float(row["routed_power_delta_w"]) for row in nonzero_place)
    zero_prediction_route_delta = sum(
        float(row["routed_power_delta_w"])
        for row in common_rows
        if abs(float(row["place_predicted_power_delta_w"])) <= numeric_epsilon
    )
    postcts_clock_delta = sum(
        float(row["routed_power_delta_w"]) for row in rows if row["routed_class"] == "postcts_clock"
    )
    route_only_nonclock_delta = sum(
        float(row["routed_power_delta_w"])
        for row in rows
        if row["routed_class"] == "not_common_to_placement"
        and row["routed_activity_origin"] != "clock"
        and not str(row["net"]).startswith("clknet_")
    )
    moved_row = next((row for row in rows if row["net"] == moved_seed_net), None)
    common_absolute_error = sum(
        abs(float(row["place_minus_routed_error_w"])) for row in common_rows
    )
    common_absolute_routed = sum(abs(float(row["routed_power_delta_w"])) for row in common_rows)
    return {
        "workload": workload,
        "base_annotated": base_annotated,
        "candidate_annotated": candidate_annotated,
        "base_unannotated": base_unannotated,
        "candidate_unannotated": candidate_unannotated,
        "baseline_place_reported_w": base_reported,
        "baseline_place_net_sum_w": place_net_baseline,
        "candidate_place_reported_w": candidate_reported,
        "candidate_place_net_sum_w": place_net_candidate,
        "place_reported_delta_w": place_report_delta,
        "place_per_net_delta_w": place_net_delta,
        "place_delta_residual_w": place_net_delta - place_report_delta,
        "route_reported_delta_w": routed_reported_delta,
        "route_per_net_delta_w": sum(float(row["routed_power_delta_w"]) for row in rows),
        "common_signal_net_count": len(common_rows),
        "common_signal_nonzero_place_predictions": len(nonzero_place),
        "common_signal_nonzero_route_changes": len(nonzero_route),
        "common_signal_sign_matches": sign_matches,
        "common_signal_sign_reversals": sign_reversals,
        "common_signal_route_change_place_zero": route_change_place_zero,
        "nonzero_prediction_place_delta_w": sum(
            float(row["place_predicted_power_delta_w"]) for row in nonzero_place
        ),
        "nonzero_prediction_routed_delta_w": predicted_net_route_delta,
        "zero_prediction_routed_delta_w": zero_prediction_route_delta,
        "common_signal_place_delta_w": common_place_delta,
        "common_signal_route_delta_w": common_route_delta,
        "common_signal_abs_error_w": common_absolute_error,
        "common_signal_abs_route_delta_w": common_absolute_routed,
        "postcts_clock_delta_w": postcts_clock_delta,
        "route_only_nonclock_delta_w": route_only_nonclock_delta,
        "moved_seed_net": moved_seed_net,
        "moved_seed_net_place_delta_w": (
            float(moved_row["place_predicted_power_delta_w"]) if moved_row else 0.0
        ),
        "moved_seed_net_route_delta_w": (
            float(moved_row["routed_power_delta_w"]) if moved_row else 0.0
        ),
        "moved_seed_net_place_wire_delta_f": (
            float(moved_row["delta_place_wire_cap_f"]) if moved_row else 0.0
        ),
        "moved_seed_net_route_wire_delta_f": (
            float(moved_row["routed_delta_wire_cap_f"]) if moved_row else 0.0
        ),
        "per_net_csv": str(per_net_path),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placement-baseline-dir", type=Path, required=True)
    parser.add_argument("--placement-candidate-dir", type=Path, required=True)
    parser.add_argument("--routed-attribution-dir", type=Path, required=True)
    parser.add_argument("--move-summary", type=Path, required=True)
    parser.add_argument("--move-eval-table", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    try:
        manifest = read_and_check_manifest(args.manifest)
        moved_instance, moved_seed_net = read_moved_instance(
            args.move_summary, args.move_eval_table
        )
        results = [
            analyze_workload(
                workload,
                args.placement_baseline_dir,
                args.placement_candidate_dir,
                args.routed_attribution_dir,
                args.output_dir,
                moved_instance,
                moved_seed_net,
            )
            for workload in WORKLOADS
        ]
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"Placement-to-SPEF calibration failed: {exc}", file=sys.stderr)
        return 2

    lines = [
        "Placement-to-routed-SPEF per-net calibration",
        "Placement power uses the exact baseline and one-move ODBs, their own gate-level VCDs, and placement-estimated capacitance.",
        "Routed values use the completed baseline/candidate SPEF attribution reports and each final netlist's VCD.",
        "Post-CTS clock nets absent from the pre-CTS placement are reported separately; they are not treated as placement-prediction errors.",
        f"Moved instance: {moved_instance}; seed net used for focused inspection: {moved_seed_net}.",
        f"Input/output integrity manifest (all before/after SHA-256 checks matched): {args.manifest}",
        "This is a comparison dataset, not a fitted correction model; one candidate is insufficient to fit reliable routing coefficients.",
        "",
        "Totals (negative power delta means reduction):",
    ]
    worst_reversal = None
    for result in results:
        workload = str(result["workload"])
        rows = result["rows"]
        assert isinstance(rows, list)
        reversals = [
            row for row in rows
            if row["routed_class"] == "common_signal"
            and float(row["place_predicted_power_delta_w"]) * float(row["routed_power_delta_w"]) < 0.0
        ]
        if reversals:
            worst = max(reversals, key=lambda row: abs(float(row["place_minus_routed_error_w"])))
            if worst_reversal is None or abs(float(worst["place_minus_routed_error_w"])) > abs(float(worst_reversal["place_minus_routed_error_w"])):
                worst_reversal = worst
        common_error = float(result["common_signal_abs_error_w"])
        common_route_abs = float(result["common_signal_abs_route_delta_w"])
        relative_error = common_error / common_route_abs if common_route_abs else 0.0
        lines.extend(
            [
                f"{workload}:",
                f"  Placement OpenSTA delta: {float(result['place_reported_delta_w']):+.6e} W; "
                f"placement per-net delta: {float(result['place_per_net_delta_w']):+.6e} W; "
                f"per-net residual: {float(result['place_delta_residual_w']):+.3e} W.",
                f"  Routed OpenSTA delta: {float(result['route_reported_delta_w']):+.6e} W; "
                f"routed per-net delta: {float(result['route_per_net_delta_w']):+.6e} W.",
                f"  Matched non-clock nets: {result['common_signal_net_count']}; "
                f"nonzero placement predictions: {result['common_signal_nonzero_place_predictions']}; "
                f"nonzero routed changes: {result['common_signal_nonzero_route_changes']}; "
                f"same-sign among predictions: {result['common_signal_sign_matches']}; "
                f"reversals: {result['common_signal_sign_reversals']}; "
                f"routed changes with near-zero placement estimate: {result['common_signal_route_change_place_zero']}; "
                f"placement/routed sums {float(result['common_signal_place_delta_w']):+.3e} / "
                f"{float(result['common_signal_route_delta_w']):+.3e} W; "
                f"weighted absolute error {common_error:.3e} W ({relative_error:.2%} of matched routed |delta|).",
                f"  Nonzero-prediction subset: placement {float(result['nonzero_prediction_place_delta_w']):+.3e} W, "
                f"same nets routed {float(result['nonzero_prediction_routed_delta_w']):+.3e} W; "
                f"routed contribution on near-zero estimates {float(result['zero_prediction_routed_delta_w']):+.3e} W.",
                f"  Routed post-CTS clock contribution: {float(result['postcts_clock_delta_w']):+.3e} W; "
                f"other routed-only non-clock contribution: {float(result['route_only_nonclock_delta_w']):+.3e} W.",
                f"  VCD annotated pins: {result['base_annotated']} / {result['candidate_annotated']}; "
                f"unannotated: {result['base_unannotated']} / {result['candidate_unannotated']}.",
                f"  Seed net {result['moved_seed_net']}: placement {float(result['moved_seed_net_place_delta_w']):+.3e} W, "
                f"routed {float(result['moved_seed_net_route_delta_w']):+.3e} W; "
                f"wire-cap delta placement {float(result['moved_seed_net_place_wire_delta_f'])*1e15:+.3f} fF, "
                f"routed {float(result['moved_seed_net_route_wire_delta_f'])*1e15:+.3f} fF.",
                f"  Per-net table: {result['per_net_csv']}",
            ]
        )
    lines.extend(["", "Largest sign-reversal nets among matched non-clock nets:"])
    if worst_reversal is None:
        lines.append("  none")
    else:
        for result in results:
            workload = str(result["workload"])
            rows = result["rows"]
            assert isinstance(rows, list)
            reversals = [
                row for row in rows
                if row["routed_class"] == "common_signal"
                and float(row["place_predicted_power_delta_w"]) * float(row["routed_power_delta_w"]) < 0.0
            ]
            for row in sorted(reversals, key=lambda item: abs(float(item["place_minus_routed_error_w"])), reverse=True)[:5]:
                lines.append(
                    f"  {workload} {row['net']}: placement {float(row['place_predicted_power_delta_w'])*1e9:+.3f} nW, "
                    f"routed {float(row['routed_power_delta_w'])*1e9:+.3f} nW, "
                    f"placement wire-cap {float(row['delta_place_wire_cap_f'])*1e15:+.3f} fF, "
                    f"routed wire-cap {float(row['routed_delta_wire_cap_f'])*1e15:+.3f} fF"
                )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.output_dir / "summary.txt"
    summary_path.write_text("\n".join(lines) + "\n")
    print(summary_path.read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
