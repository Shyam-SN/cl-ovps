#!/usr/bin/env python3
"""Compare pre-route placement/clock-geometry scores with routed dynamic power."""

from __future__ import annotations

import argparse
import csv
import math
import re
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
POWER_TOTAL_RE = re.compile(
    r"^Total\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)",
    re.MULTILINE,
)
POWER_CLOCK_RE = re.compile(
    r"^Clock\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)",
    re.MULTILINE,
)
TIMING_RE = re.compile(
    r"Timing slack: setup ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns; "
    r"hold ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns"
)
ANNOTATION_RE = re.compile(
    r"Activity annotated: (\d+) baseline / (\d+) candidate; "
    r"unannotated: (\d+) / (\d+)"
)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=list(rows[0]), delimiter="\t", lineterminator="\n"
        )
        writer.writeheader()
        writer.writerows(rows)


def power_total(path: Path) -> dict[str, float]:
    match = POWER_TOTAL_RE.search(path.read_text())
    if match is None:
        raise ValueError(f"could not parse OpenSTA Total power row from {path}")
    internal, switching, leakage, total = (float(value) for value in match.groups())
    if abs((internal + switching + leakage) - total) > max(5e-12, total * 1e-7):
        raise ValueError(f"OpenSTA power components do not sum to total in {path}")
    result = {
        "internal_w": internal,
        "switching_w": switching,
        "leakage_w": leakage,
        "total_w": total,
    }
    clock_match = POWER_CLOCK_RE.search(path.read_text())
    if clock_match is None:
        raise ValueError(f"could not parse OpenSTA Clock power row from {path}")
    clock_internal, clock_switching, clock_leakage, clock_total = (
        float(value) for value in clock_match.groups()
    )
    if abs((clock_internal + clock_switching + clock_leakage) - clock_total) > max(5e-12, clock_total * 1e-7):
        raise ValueError(f"OpenSTA Clock power components do not sum to total in {path}")
    result.update({
        "clock_internal_w": clock_internal,
        "clock_switching_w": clock_switching,
        "clock_leakage_w": clock_leakage,
        "clock_total_w": clock_total,
    })
    return result


def spef_switching_parts(path: Path) -> tuple[dict[str, float], float]:
    by_origin = {"clock": 0.0, "signal": 0.0}
    by_pin: dict[str, tuple[float, str]] = {}
    for line in path.read_text().splitlines():
        if not line.startswith("#SPEF_DRIVER_CAP\t"):
            continue
        fields = line.split("\t")
        if len(fields) == 9 and fields[1] == "pin":
            continue
        if len(fields) != 9:
            raise ValueError(f"malformed driver-cap row in {path}: {line}")
        _, pin, _net, _pin_cap, _wire_cap, total_cap, density_hz, _duty, origin = fields
        contribution = 0.5 * VOLTAGE**2 * float(total_cap) * float(density_hz)
        if origin == "clock":
            by_origin["clock"] += contribution
        else:
            by_origin["signal"] += contribution
        by_pin[pin] = (contribution, origin)
    if not by_pin:
        raise ValueError(f"no per-driver SPEF power rows in {path}")
    return by_origin, sum(item[0] for item in by_pin.values())


def sink_geometry(points: list[tuple[float, float]]) -> dict[str, float]:
    if len(points) < 2:
        raise ValueError("clock sink geometry requires at least two sequential cells")
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    center_x = sum(xs) / len(xs)
    center_y = sum(ys) / len(ys)
    dispersion = sum((x - center_x) ** 2 + (y - center_y) ** 2 for x, y in points) / len(points)

    remaining = set(range(1, len(points)))
    connected = {0}
    mst = 0.0
    while remaining:
        distance, index = min(
            (
                math.hypot(points[left][0] - points[right][0], points[left][1] - points[right][1]),
                right,
            )
            for right in remaining
            for left in connected
        )
        mst += distance
        remaining.remove(index)
        connected.add(index)

    nearest = []
    for index, point in enumerate(points):
        nearest.append(min(
            math.hypot(point[0] - other[0], point[1] - other[1])
            for other_index, other in enumerate(points) if other_index != index
        ))
    return {
        "hpwl_um": (max(xs) - min(xs) + max(ys) - min(ys)) / 1000.0,
        "dispersion_um2": dispersion / 1e6,
        "mst_um": mst / 1000.0,
        "mean_nearest_um": (sum(nearest) / len(nearest)) / 1000.0,
    }


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    output = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for position in order[index:end]:
            output[position] = rank
        index = end
    return output


def pearson(x: list[float], y: list[float]) -> float:
    xbar = sum(x) / len(x)
    ybar = sum(y) / len(y)
    numerator = sum((a - xbar) * (b - ybar) for a, b in zip(x, y))
    denominator = math.sqrt(
        sum((a - xbar) ** 2 for a in x) * sum((b - ybar) ** 2 for b in y)
    )
    return numerator / denominator if denominator else float("nan")


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(ranks(x), ranks(y))


def kendall_tau_b(x: list[float], y: list[float]) -> float:
    concordant = discordant = tied_x = tied_y = 0
    for i in range(len(x)):
        for j in range(i + 1, len(x)):
            dx = (x[i] > x[j]) - (x[i] < x[j])
            dy = (y[i] > y[j]) - (y[i] < y[j])
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                tied_x += 1
            elif dy == 0:
                tied_y += 1
            elif dx == dy:
                concordant += 1
            else:
                discordant += 1
    denominator = math.sqrt(
        (concordant + discordant + tied_x) * (concordant + discordant + tied_y)
    )
    return (concordant - discordant) / denominator if denominator else float("nan")


def fmt(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placement-audit", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--occupancy-inventory", type=Path, required=True)
    parser.add_argument("--route-manifest", type=Path, required=True)
    parser.add_argument("--routes-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    audit = {row["candidate_id"]: row for row in read_tsv(args.placement_audit)}
    proposals = {row["candidate_id"]: row for row in read_tsv(args.pool)}
    inventory = read_tsv(args.occupancy_inventory)
    placed_flops = [
        row for row in inventory
        if row["status"] == "PLACED"
        and ("dfxtp" in row["master"] or "edfxtp" in row["master"])
    ]
    base_points = [
        (int(row["x_dbu"]) + int(row["width_dbu"]) / 2,
         int(row["y_dbu"]) + int(row["height_dbu"]) / 2)
        for row in placed_flops
    ]
    base_geometry = sink_geometry(base_points)

    route_rows = read_tsv(args.route_manifest)
    metric_rows: list[dict[str, object]] = []
    for route in route_rows:
        candidate = route["candidate_id"]
        if int(route["return_code"]) != 0:
            continue
        if candidate not in audit or candidate not in proposals:
            raise ValueError(f"route candidate {candidate} missing from placement audit/pool")
        proposal = proposals[candidate]
        screen = audit[candidate]
        points = list(base_points)
        for index, flop in enumerate(placed_flops):
            if flop["instance"] == proposal["instance"]:
                points[index] = (
                    int(proposal["target_x_dbu"]) + int(flop["width_dbu"]) / 2,
                    int(proposal["target_y_dbu"]) + int(flop["height_dbu"]) / 2,
                )
                break
        else:
            raise ValueError(f"candidate {candidate} does not refer to a placed clock sink")
        geometry = sink_geometry(points)

        route_dir = args.routes_root / route["route_variant"]
        summary_text = (route_dir / "summary.txt").read_text()
        timing_match = TIMING_RE.search(summary_text)
        annotation_matches = ANNOTATION_RE.findall(summary_text)
        if timing_match is None or len(annotation_matches) != len(WORKLOADS):
            raise ValueError(f"routed timing/activity summary is incomplete for {candidate}")
        setup_before, setup_after, hold_before, hold_after = map(float, timing_match.groups())
        guard_match = re.search(r"Setup/hold guard \(50 ps\): (PASS|FAIL)\.", summary_text)
        if guard_match is None:
            raise ValueError(f"routed timing guard is absent from {candidate} summary")
        annotation_ok = all(
            int(baseline) == 978 and int(candidate_pins) == 978
            and int(unannotated_baseline) == 0 and int(unannotated_candidate) == 0
            for baseline, candidate_pins, unannotated_baseline, unannotated_candidate in annotation_matches
        )
        result: dict[str, object] = {
            "candidate_id": candidate,
            "instance": proposal["instance"],
            "from_x_dbu": proposal["from_x_dbu"],
            "from_y_dbu": proposal["from_y_dbu"],
            "to_x_dbu": proposal["target_x_dbu"],
            "to_y_dbu": proposal["target_y_dbu"],
            "displacement_um": math.hypot(
                int(proposal["target_x_dbu"]) - int(proposal["from_x_dbu"]),
                int(proposal["target_y_dbu"]) - int(proposal["from_y_dbu"]),
            ) / 1000.0,
            "unique_flops_in_pool": len({p["instance"] for p in proposals.values()}),
            "setup_loss_ps": (float(screen["setup_before_ns"]) - float(screen["setup_after_ns"])) * 1000.0,
            "hold_loss_ps": (float(screen["hold_before_ns"]) - float(screen["hold_after_ns"])) * 1000.0,
            "routed_setup_loss_ps": (setup_before - setup_after) * 1000.0,
            "routed_hold_loss_ps": (hold_before - hold_after) * 1000.0,
            "routed_timing_guard": guard_match.group(1),
            "activity_annotation_complete": "YES" if annotation_ok else "NO",
        }
        for name, value in geometry.items():
            result[f"delta_{name}"] = value - base_geometry[name]

        for workload in WORKLOADS:
            before = power_total(route_dir / "baseline" / f"{workload}.log")
            after = power_total(route_dir / "candidate" / f"{workload}.log")
            for component in ("internal", "switching", "leakage", "total"):
                result[f"routed_{workload}_{component}_delta_nw"] = (
                    after[f"{component}_w"] - before[f"{component}_w"]
                ) * 1e9
            for component in ("clock_internal", "clock_switching", "clock_leakage", "clock_total"):
                result[f"routed_{workload}_{component}_delta_nw"] = (
                    after[f"{component}_w"] - before[f"{component}_w"]
                ) * 1e9
            result[f"routed_{workload}_dynamic_delta_nw"] = (
                result[f"routed_{workload}_internal_delta_nw"]
                + result[f"routed_{workload}_switching_delta_nw"]
            )
            baseline_dynamic_w = before["internal_w"] + before["switching_w"]
            result[f"routed_{workload}_baseline_dynamic_uw"] = baseline_dynamic_w * 1e6
            result[f"routed_{workload}_dynamic_delta_pct"] = (
                float(result[f"routed_{workload}_dynamic_delta_nw"]) / (baseline_dynamic_w * 1e9) * 100.0
            )
            before_parts, before_formula = spef_switching_parts(
                route_dir / "baseline" / f"{workload}.driver_caps.tsv"
            )
            after_parts, after_formula = spef_switching_parts(
                route_dir / "candidate" / f"{workload}.driver_caps.tsv"
            )
            if before_formula and after_formula:
                formula_delta = after_formula - before_formula
                reported_delta = result[f"routed_{workload}_switching_delta_nw"]
                if abs(formula_delta * 1e9 - reported_delta) > max(2.0, abs(reported_delta) * 0.01):
                    raise ValueError(f"SPEF switching components fail power reconciliation for {candidate}/{workload}")
            result[f"routed_{workload}_clock_switching_delta_nw"] = (
                after_parts["clock"] - before_parts["clock"]
            ) * 1e9
            result[f"routed_{workload}_signal_switching_delta_nw"] = (
                after_parts["signal"] - before_parts["signal"]
            ) * 1e9
            result[f"placement_{workload}_switching_delta_nw"] = float(screen[f"{workload}_delta_w"]) * 1e9
            result[f"placement_{workload}_internal_delta_nw"] = float(screen[f"{workload}_internal_delta_w"]) * 1e9
            result[f"placement_{workload}_dynamic_delta_nw"] = float(screen[f"{workload}_dynamic_delta_w"]) * 1e9
        metric_rows.append(result)

    if not metric_rows:
        raise SystemExit("route manifest contains no successful candidate routes")

    predictors = {
        "placement_dynamic": (
            lambda row, workload: float(row[f"placement_{workload}_dynamic_delta_nw"]),
            lambda row, workload: float(row[f"routed_{workload}_dynamic_delta_nw"]),
            "routed_internal_plus_switching_delta_nw",
        ),
        "placement_net_switching": (
            lambda row, workload: float(row[f"placement_{workload}_switching_delta_nw"]),
            lambda row, workload: float(row[f"routed_{workload}_signal_switching_delta_nw"]),
            "routed_signal_switching_delta_nw",
        ),
    }
    for geometry_name, column in (
        ("clock_sink_hpwl", "delta_hpwl_um"),
        ("clock_sink_mst", "delta_mst_um"),
        ("clock_sink_dispersion", "delta_dispersion_um2"),
        ("clock_sink_nearest_neighbor", "delta_mean_nearest_um"),
    ):
        predictors[f"{geometry_name}_vs_clock_internal"] = (
            lambda row, workload, column=column: float(row[column]),
            lambda row, workload: float(row[f"routed_{workload}_clock_internal_delta_nw"]),
            "routed_clock_internal_delta_nw",
        )
        predictors[f"{geometry_name}_vs_clock_net_switching"] = (
            lambda row, workload, column=column: float(row[column]),
            lambda row, workload: float(row[f"routed_{workload}_clock_switching_delta_nw"]),
            "routed_clock_switching_delta_nw",
        )
    correlations: list[dict[str, object]] = []
    for workload in WORKLOADS:
        for predictor, (feature_getter, target_getter, target_name) in predictors.items():
            values = [feature_getter(row, workload) for row in metric_rows]
            target = [target_getter(row, workload) for row in metric_rows]
            correlations.append({
                "workload": workload,
                "target": target_name,
                "predictor": predictor,
                "candidate_count": len(metric_rows),
                "spearman_rho": spearman(values, target),
                "kendall_tau_b": kendall_tau_b(values, target),
            })

    write_tsv(args.output_dir / "candidate_metrics.tsv", metric_rows)
    write_tsv(args.output_dir / "rank_correlations.tsv", correlations)
    all_workload_improvements = sum(
        all(float(row[f"routed_{workload}_dynamic_delta_nw"]) < 0 for workload in WORKLOADS)
        for row in metric_rows
    )
    routed_timing_pass = sum(row["routed_timing_guard"] == "PASS" for row in metric_rows)
    annotation_pass = sum(row["activity_annotation_complete"] == "YES" for row in metric_rows)
    best = min(metric_rows, key=lambda row: max(
        float(row[f"routed_{workload}_dynamic_delta_nw"]) for workload in WORKLOADS
    ))
    lines = [
        "# Randomized GCD relocation ranking holdout",
        "",
        f"- Routed candidates: {len(metric_rows)}.",
        f"- Unique sequential sinks represented: {len({p['instance'] for p in proposals.values()})}.",
        f"- Candidates reducing internal + net switching in all three workloads: {all_workload_improvements}.",
        f"- Routed 50 ps timing guard passed: {routed_timing_pass}/{len(metric_rows)}.",
        f"- Full 978-pin VCD annotation in all routed workloads: {annotation_pass}/{len(metric_rows)}.",
        "- Leakage is reported separately and excluded from the dynamic-power target.",
        "- Sink geometry treats each placed sequential cell as a sink in the single GCD clock domain.",
        "",
        "## Rank correlations",
        "",
        "Positive correlation means a larger predicted delta follows a larger routed delta for the target component.",
        "",
        "| Workload | Predictor | Spearman ρ | Kendall τ-b | N |",
        "|---|---|---:|---:|---:|",
    ]
    for row in correlations:
        lines.append(
            f"| {row['workload']} | {row['predictor']} | {fmt(float(row['spearman_rho']))} "
            f"| {fmt(float(row['kendall_tau_b']))} | {row['candidate_count']} |"
        )
    lines.extend([
        "",
        "## Best worst-workload candidate",
        "",
        f"`{best['candidate_id']}` ({best['instance']}) has routed dynamic deltas: "
        + ", ".join(
            f"{workload} {float(best[f'routed_{workload}_dynamic_delta_nw']):+.2f} nW "
            f"({float(best[f'routed_{workload}_dynamic_delta_pct']):+.3f}%)"
            for workload in WORKLOADS
        ) + ".",
        "",
        "Its clock-group internal-power change is "
        + ", ".join(
            f"{workload} {float(best[f'routed_{workload}_clock_internal_delta_nw']):+.2f} nW"
            for workload in WORKLOADS
        ) + "; clock-net switching is "
        + ", ".join(
            f"{workload} {float(best[f'routed_{workload}_clock_switching_delta_nw']):+.2f} nW"
            for workload in WORKLOADS
        ) + ".",
        "",
        "Candidate-level metrics, including internal/switching/leakage splits and geometric features, "
        "are in `candidate_metrics.tsv`; the rank table is in `rank_correlations.tsv`.",
    ])
    summary = args.output_dir / "summary.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("\n".join(lines) + "\n")
    print(f"GCD_RELOCATION_RANKING candidates={len(metric_rows)} output={summary}")


if __name__ == "__main__":
    main()
