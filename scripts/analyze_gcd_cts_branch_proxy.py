#!/usr/bin/env python3
"""Evaluate baseline-CTS-centroid proxies on a frozen GCD relocation pool."""

from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import re
import subprocess
from pathlib import Path


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
            stream,
            fieldnames=list(rows[0]),
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def inventory(path: Path) -> dict[str, dict[str, object]]:
    rows = read_tsv(path)
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        result[row["instance"]] = {
            "master": row["master"],
            "branch": row["branch"],
            "x": int(row["x_dbu"]) / 1000.0,
            "y": int(row["y_dbu"]) / 1000.0,
            "cap": float(row["cap_ff"]),
        }
    return result


def rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        average = (index + 1 + end) / 2.0
        for offset in range(index, end):
            ranks[order[offset]] = average
        index = end
    return ranks


def pearson(x: list[float], y: list[float]) -> float:
    if len(x) != len(y) or not x:
        return float("nan")
    xbar = sum(x) / len(x)
    ybar = sum(y) / len(y)
    numerator = sum((a - xbar) * (b - ybar) for a, b in zip(x, y))
    denominator = math.sqrt(
        sum((a - xbar) ** 2 for a in x) * sum((b - ybar) ** 2 for b in y)
    )
    return numerator / denominator if denominator else float("nan")


def spearman(x: list[float], y: list[float]) -> float:
    return pearson(rank(x), rank(y))


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


def branch_centroids(
    sinks: dict[str, dict[str, object]],
) -> tuple[dict[str, tuple[float, float]], dict[str, float]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in sinks.values():
        grouped.setdefault(str(item["branch"]), []).append(item)
    centers = {
        branch: (
            sum(float(item["x"]) for item in members) / len(members),
            sum(float(item["y"]) for item in members) / len(members),
        )
        for branch, members in grouped.items()
    }
    loads = {
        branch: sum(float(item["cap"]) for item in members)
        for branch, members in grouped.items()
    }
    return centers, loads


def sibling_dummy_rows(
    candidate_id: str,
    sinks: dict[str, dict[str, object]],
    netlist: Path,
    dummy_caps_ff: dict[str, float],
    branches: list[str],
) -> list[dict[str, object]]:
    _, loads = branch_centroids(sinks)
    counts = {
        branch: sum(item["branch"] == branch for item in sinks.values())
        for branch in branches
    }
    if set(loads) != set(branches):
        raise ValueError(f"{candidate_id} changed the CTS leaf-branch set")
    sibling_max_ff = max(loads.values())
    dummy_by_branch = dummy_loads_by_branch(netlist)
    rows: list[dict[str, object]] = []
    for branch in branches:
        deficit_ff = sibling_max_ff - loads[branch]
        observed = dummy_by_branch.get(branch)
        observed_instance, observed_master = observed if observed else ("", "")
        observed_cap_ff = dummy_caps_ff.get(observed_master, float("nan"))
        observed_error_ff = (
            abs(observed_cap_ff - deficit_ff) if observed else float("nan")
        )
        nearest_master = min(
            dummy_caps_ff,
            key=lambda master: abs(dummy_caps_ff[master] - deficit_ff),
        )
        nearest_error_ff = abs(dummy_caps_ff[nearest_master] - deficit_ff)
        rows.append({
            "candidate_id": candidate_id,
            "branch": branch,
            "sink_count": counts[branch],
            "child_output_cap_ff": loads[branch],
            "max_sibling_output_cap_ff": sibling_max_ff,
            "dummy_cap_deficit_ff": deficit_ff,
            "observed_dummy_instance": observed_instance,
            "observed_dummy_master": observed_master,
            "observed_dummy_input_cap_ff": observed_cap_ff,
            "observed_master_cap_error_ff": observed_error_ff,
            "nearest_among_observed_masters": nearest_master,
            "nearest_observed_master_cap_error_ff": nearest_error_ff,
            "observed_matches_nearest_observed_master": int(
                bool(observed) and observed_master == nearest_master
            ),
        })
    return rows


def assign_nearest(
    sinks: dict[str, dict[str, object]], centers: dict[str, tuple[float, float]]
) -> dict[str, str]:
    return {
        inst: min(
            centers,
            key=lambda branch: math.hypot(
                float(item["x"]) - centers[branch][0],
                float(item["y"]) - centers[branch][1],
            ),
        )
        for inst, item in sinks.items()
    }


def loads_by_assignment(
    sinks: dict[str, dict[str, object]], assignment: dict[str, str], branches: list[str]
) -> dict[str, float]:
    loads = {branch: 0.0 for branch in branches}
    for inst, branch in assignment.items():
        loads[branch] += float(sinks[inst]["cap"])
    return loads


def variance(values: list[float]) -> float:
    mean = sum(values) / len(values)
    return sum((value - mean) ** 2 for value in values) / len(values)


def cts_cell_masters(netlist: Path) -> dict[str, str]:
    pattern = re.compile(r"^\s*(\S+)\s+((?:clkload\d+)|(?:clkbuf_[^\s]+))\s*\(", re.MULTILINE)
    return {instance: master for master, instance in pattern.findall(netlist.read_text())}


def dummy_loads_by_branch(netlist: Path) -> dict[str, tuple[str, str]]:
    pattern = re.compile(
        r"^\s*(sky130_fd_sc_hd__(?:clkbuf|clkinv)_\d+)\s+"
        r"(clkload\d+)\s+\(\.A\(([^)]+)\)\);",
        re.MULTILINE,
    )
    return {
        branch: (instance, master)
        for master, instance, branch in pattern.findall(netlist.read_text())
    }


def branch_mapping(
    baseline: dict[str, dict[str, object]], candidate: dict[str, dict[str, object]]
) -> dict[str, str]:
    base_branches = sorted({str(item["branch"]) for item in baseline.values()})
    cand_branches = sorted({str(item["branch"]) for item in candidate.values()})
    overlap = {
        (cand, base): sum(
            1
            for inst in baseline.keys() & candidate.keys()
            if str(candidate[inst]["branch"]) == cand
            and str(baseline[inst]["branch"]) == base
        )
        for cand in cand_branches
        for base in base_branches
    }
    if len(cand_branches) == len(base_branches):
        return max(
            (
                dict(zip(cand_branches, perm))
                for perm in itertools.permutations(base_branches)
            ),
            key=lambda mapping: sum(overlap[(cand, base)] for cand, base in mapping.items()),
        )
    mapping: dict[str, str] = {}
    available = set(base_branches)
    for cand in sorted(cand_branches, key=lambda c: -max(overlap[(c, b)] for b in base_branches)):
        best = max(available, key=lambda base: overlap[(cand, base)])
        mapping[cand] = best
        available.remove(best)
    return mapping


def extract_inventory(openroad: Path, tcl: Path, odb: Path, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CTS_SINK_ODB"] = str(odb.resolve())
    env["CTS_SINK_OUT"] = str(out.resolve())
    process = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(tcl)],
        cwd=tcl.parent.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    if process.returncode:
        raise RuntimeError(
            f"OpenROAD inventory extraction failed for {odb}:\n"
            f"{process.stdout}\n{process.stderr}"
        )
    if not out.is_file():
        raise RuntimeError(f"OpenROAD produced no inventory for {odb}")


def extract_dummy_cell_caps(openroad: Path, tcl: Path, out: Path) -> dict[str, float]:
    out.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CTS_DUMMY_CAP_OUT"] = str(out.resolve())
    process = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(tcl)],
        cwd=tcl.parent.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    if process.returncode:
        raise RuntimeError(
            f"OpenROAD dummy-cap extraction failed:\n{process.stdout}\n{process.stderr}"
        )
    return {
        row["master"]: float(row["input_cap_ff"])
        for row in read_tsv(out)
    }


def extract_cts_cell_power(
    openroad: Path,
    tcl: Path,
    result_dir: Path,
    vcd: Path,
    log_path: Path,
) -> dict[str, tuple[float, float, float, float]]:
    env = os.environ.copy()
    env.update({
        "SPEF_ODB": str((result_dir / "6_final.odb").resolve()),
        "SPEF_SDC": str((result_dir / "6_final.sdc").resolve()),
        "SPEF_FILE": str((result_dir / "6_final.spef").resolve()),
        "ACTIVITY_VCD": str(vcd.resolve()),
        "WORKLOAD": "idle",
    })
    process = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(tcl)],
        cwd=tcl.parent.parent,
        env=env,
        text=True,
        capture_output=True,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout + process.stderr)
    if process.returncode:
        raise RuntimeError(f"CTS cell power report failed for {result_dir}: {log_path}")
    pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+(clk(?:buf|load)[^\s]*)$",
        re.MULTILINE,
    )
    values = {
        match.group(5): tuple(float(match.group(i)) for i in range(1, 5))
        for match in pattern.finditer(process.stdout)
    }
    if not values:
        raise RuntimeError(f"No per-instance CTS power rows parsed from {log_path}")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pool-dir",
        type=Path,
        default=Path("build/power/gcd_flop_relocation_random_local_20261029b"),
    )
    parser.add_argument(
        "--results-root", type=Path, default=Path("build/orfs/results/sky130hd/gcd_power")
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path("build/power/gcd_flop_relocation_random_local_20261029b/analysis/branch_proxy"),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    pool_dir = (root / args.pool_dir).resolve()
    results_root = (root / args.results_root).resolve()
    output_dir = (root / args.output_dir).resolve()
    route_data_dir = pool_dir / "analysis"
    metrics_rows = read_tsv(route_data_dir / "candidate_metrics.tsv")
    manifest_rows = {row["candidate_id"]: row for row in read_tsv(pool_dir / "route_manifest.tsv")}
    openroad = root / "tools/openroad/build/bin/openroad"
    tcl = root / "scripts/export_gcd_cts_sink_inventory.tcl"
    dummy_cap_tcl = root / "scripts/export_gcd_dummy_cell_caps.tcl"
    if not openroad.is_file():
        raise FileNotFoundError(f"OpenROAD binary not found: {openroad}")

    inventory_dir = output_dir / "inventories"
    baseline_place_path = inventory_dir / "baseline_place.tsv"
    baseline_cts_path = inventory_dir / "baseline_cts.tsv"
    extract_inventory(
        openroad,
        tcl,
        results_root / "gcd_baseline20ns/3_5_place_dp.odb",
        baseline_place_path,
    )
    extract_inventory(
        openroad,
        tcl,
        results_root / "gcd_baseline20ns/4_1_cts.odb",
        baseline_cts_path,
    )
    base_place = inventory(baseline_place_path)
    base_cts = inventory(baseline_cts_path)
    centers, actual_base_loads = branch_centroids(base_cts)
    base_branches = sorted(centers)
    base_nearest = assign_nearest(base_cts, centers)
    base_nearest_loads = loads_by_assignment(base_cts, base_nearest, base_branches)
    base_actual_load_variance = variance(list(actual_base_loads.values()))
    baseline_cts_masters = cts_cell_masters(results_root / "gcd_baseline20ns/6_final.v")
    dummy_caps_ff = extract_dummy_cell_caps(
        openroad, dummy_cap_tcl, output_dir / "dummy_cell_input_caps.tsv"
    )
    dummy_balance_rows = sibling_dummy_rows(
        "baseline",
        base_cts,
        results_root / "gcd_baseline20ns/6_final.v",
        dummy_caps_ff,
        base_branches,
    )
    cell_power_tcl = root / "scripts/gcd_report_cts_cell_power.tcl"
    cell_power_dir = output_dir / "clock_cell_power"
    baseline_cell_power = extract_cts_cell_power(
        openroad,
        cell_power_tcl,
        results_root / "gcd_baseline20ns",
        root / "build/activity/gcd_baseline20ns/routed/idle.vcd",
        cell_power_dir / "baseline_idle.log",
    )
    baseline_buffer_internal = sum(
        power[0] for name, power in baseline_cell_power.items() if name.startswith("clkbuf")
    )
    baseline_dummy_internal = sum(
        power[0] for name, power in baseline_cell_power.items() if name.startswith("clkload")
    )

    actual_cluster_rows: list[dict[str, object]] = []
    for branch in base_branches:
        members = [item for item in base_cts.values() if item["branch"] == branch]
        actual_cluster_rows.append({
            "branch": branch,
            "sink_count": len(members),
            "pin_load_ff": actual_base_loads[branch],
            "centroid_x_um": centers[branch][0],
            "centroid_y_um": centers[branch][1],
            "nearest_centroid_pin_load_ff": base_nearest_loads[branch],
        })
    write_tsv(output_dir / "baseline_cts_clusters.tsv", actual_cluster_rows)

    features: list[dict[str, object]] = []
    cts_power_attribution: list[dict[str, object]] = []
    cts_instance_rows: list[dict[str, object]] = []
    for cell, power in sorted(baseline_cell_power.items()):
        cts_instance_rows.append({
            "candidate_id": "baseline",
            "instance": cell,
            "master": baseline_cts_masters.get(cell, ""),
            "internal_nw": power[0] * 1e9,
            "switching_nw": power[1] * 1e9,
            "leakage_nw": power[2] * 1e9,
            "total_nw": power[3] * 1e9,
        })
    for row in metrics_rows:
        candidate_id = row["candidate_id"]
        candidate_place_path = inventory_dir / f"{candidate_id}_place.tsv"
        candidate_cts_path = inventory_dir / f"{candidate_id}_cts.tsv"
        extract_inventory(
            openroad,
            tcl,
            pool_dir / "candidate_odb" / f"{candidate_id}.odb",
            candidate_place_path,
        )
        route_variant = manifest_rows[candidate_id]["route_variant"]
        extract_inventory(
            openroad,
            tcl,
            results_root / route_variant / "4_1_cts.odb",
            candidate_cts_path,
        )
        candidate_place = inventory(candidate_place_path)
        candidate_cts = inventory(candidate_cts_path)
        moved = row["instance"]
        if moved not in base_cts or moved not in candidate_place or moved not in candidate_cts:
            raise ValueError(f"candidate {candidate_id} is missing sink {moved}")

        baseline_branch = str(base_cts[moved]["branch"])
        x_old = float(base_cts[moved]["x"])
        y_old = float(base_cts[moved]["y"])
        x_new = float(candidate_place[moved]["x"])
        y_new = float(candidate_place[moved]["y"])
        old_distances = {
            branch: math.hypot(x_old - cx, y_old - cy)
            for branch, (cx, cy) in centers.items()
        }
        new_distances = {
            branch: math.hypot(x_new - cx, y_new - cy)
            for branch, (cx, cy) in centers.items()
        }
        nearest_old = min(old_distances, key=old_distances.get)
        nearest_new = min(new_distances, key=new_distances.get)
        old_margin = min(
            distance for branch, distance in old_distances.items() if branch != nearest_old
        ) - old_distances[nearest_old]
        new_margin = min(
            distance for branch, distance in new_distances.items() if branch != nearest_new
        ) - new_distances[nearest_new]

        predicted_assignment = assign_nearest(candidate_place, centers)
        predicted_loads = loads_by_assignment(candidate_place, predicted_assignment, base_branches)
        predicted_radius2 = sum(
            math.hypot(
                float(item["x"]) - centers[predicted_assignment[inst]][0],
                float(item["y"]) - centers[predicted_assignment[inst]][1],
            ) ** 2
            for inst, item in candidate_place.items()
        )
        base_radius2 = sum(
            math.hypot(
                float(item["x"]) - centers[base_nearest[inst]][0],
                float(item["y"]) - centers[base_nearest[inst]][1],
            ) ** 2
            for inst, item in base_cts.items()
        )
        base_load_variance = variance([base_nearest_loads[b] for b in base_branches])
        pred_load_variance = variance([predicted_loads[b] for b in base_branches])

        actual_map = branch_mapping(base_cts, candidate_cts)
        actual_assignment = {
            inst: actual_map[str(item["branch"])] for inst, item in candidate_cts.items()
        }
        actual_churn = sum(
            actual_assignment[inst] != str(base_cts[inst]["branch"])
            for inst in base_cts.keys() & actual_assignment.keys()
        )
        actual_branch = actual_assignment[moved]
        actual_loads = loads_by_assignment(candidate_cts, actual_assignment, base_branches)
        actual_load_variance = variance(list(actual_loads.values()))
        candidate_netlist = results_root / route_variant / "6_final.v"
        candidate_cts_masters = cts_cell_masters(candidate_netlist)
        dummy_balance_rows.extend(sibling_dummy_rows(
            candidate_id,
            candidate_cts,
            candidate_netlist,
            dummy_caps_ff,
            base_branches,
        ))
        dummy_master_changes = {
            instance: (baseline_cts_masters.get(instance), master)
            for instance, master in candidate_cts_masters.items()
            if instance.startswith("clkload") and baseline_cts_masters.get(instance) != master
        }

        result_dir = results_root / route_variant
        candidate_cell_power = extract_cts_cell_power(
            openroad,
            cell_power_tcl,
            result_dir,
            root / "build/activity" / route_variant / "candidate/idle.vcd",
            cell_power_dir / f"{candidate_id}_idle.log",
        )
        candidate_buffer_internal = sum(
            power[0] for name, power in candidate_cell_power.items() if name.startswith("clkbuf")
        )
        candidate_dummy_internal = sum(
            power[0] for name, power in candidate_cell_power.items() if name.startswith("clkload")
        )
        for cell, power in sorted(candidate_cell_power.items()):
            cts_instance_rows.append({
                "candidate_id": candidate_id,
                "instance": cell,
                "master": candidate_cts_masters.get(cell, ""),
                "internal_nw": power[0] * 1e9,
                "switching_nw": power[1] * 1e9,
                "leakage_nw": power[2] * 1e9,
                "total_nw": power[3] * 1e9,
            })
        target_clock_internal_nw = float(row["routed_idle_clock_internal_delta_nw"])
        measured_clock_cell_internal_delta_nw = (
            candidate_buffer_internal + candidate_dummy_internal
            - baseline_buffer_internal - baseline_dummy_internal
        ) * 1e9
        cts_power_attribution.append({
            "candidate_id": candidate_id,
            "baseline_clock_group_internal_delta_nw": target_clock_internal_nw,
            "cts_buffer_internal_delta_nw": (candidate_buffer_internal - baseline_buffer_internal) * 1e9,
            "cts_dummy_load_internal_delta_nw": (candidate_dummy_internal - baseline_dummy_internal) * 1e9,
            "cts_instance_internal_sum_delta_nw": measured_clock_cell_internal_delta_nw,
            "unattributed_clock_group_internal_delta_nw": target_clock_internal_nw - measured_clock_cell_internal_delta_nw,
            "dummy_load_master_changes": ";".join(
                f"{instance}:{old}->{new}" for instance, (old, new) in sorted(dummy_master_changes.items())
            ),
        })

        # Verify that the screened ODB differs from baseline at only the proposed sink.
        changed_positions = sum(
            math.hypot(
                float(candidate_place[inst]["x"]) - float(base_place[inst]["x"]),
                float(candidate_place[inst]["y"]) - float(base_place[inst]["y"]),
            ) > 0.001
            for inst in base_place.keys() & candidate_place.keys()
        )

        features.append({
            "candidate_id": candidate_id,
            "instance": moved,
            "moved_sink_count_in_placement_odb": changed_positions,
            "baseline_cts_branch": baseline_branch,
            "nearest_branch_at_old_location": nearest_old,
            "nearest_branch_at_new_location": nearest_new,
            "nearest_branch_changed": int(nearest_old != nearest_new),
            "distance_to_original_centroid_old_um": old_distances[baseline_branch],
            "distance_to_original_centroid_new_um": new_distances[baseline_branch],
            "delta_distance_to_original_centroid_um": new_distances[baseline_branch] - old_distances[baseline_branch],
            "delta_nearest_centroid_distance_um": min(new_distances.values()) - min(old_distances.values()),
            "nearest_centroid_margin_old_um": old_margin,
            "nearest_centroid_margin_new_um": new_margin,
            "delta_nearest_centroid_margin_um": new_margin - old_margin,
            "delta_nearest_assignment_load_variance_ff2": pred_load_variance - base_load_variance,
            "delta_nearest_assignment_max_load_ff": max(predicted_loads.values()) - max(base_nearest_loads.values()),
            "delta_nearest_assignment_radius2_um2": (predicted_radius2 - base_radius2) / 1000.0,
            "actual_cts_branch": actual_branch,
            "actual_branch_changed": int(actual_branch != baseline_branch),
            "actual_branch_membership_churn_sinks": actual_churn,
            "delta_actual_cts_load_variance_ff2": actual_load_variance - base_actual_load_variance,
            "actual_cts_load_variance_ff2": actual_load_variance,
            "dummy_load_master_change_count": len(dummy_master_changes),
            "dummy_load_master_changes": ";".join(
                f"{instance}:{old}->{new}" for instance, (old, new) in sorted(dummy_master_changes.items())
            ),
            "routed_idle_clock_internal_delta_nw": float(row["routed_idle_clock_internal_delta_nw"]),
            "routed_typical_clock_internal_delta_nw": float(row["routed_typical_clock_internal_delta_nw"]),
            "routed_peak_clock_internal_delta_nw": float(row["routed_peak_clock_internal_delta_nw"]),
            "routed_idle_dynamic_delta_nw": float(row["routed_idle_dynamic_delta_nw"]),
            "routed_typical_dynamic_delta_nw": float(row["routed_typical_dynamic_delta_nw"]),
            "routed_peak_dynamic_delta_nw": float(row["routed_peak_dynamic_delta_nw"]),
        })

    write_tsv(output_dir / "candidate_branch_features.tsv", features)
    write_tsv(output_dir / "sibling_dummy_deficits.tsv", dummy_balance_rows)
    write_tsv(output_dir / "candidate_cts_instance_power.tsv", cts_instance_rows)
    write_tsv(output_dir / "clock_cell_power_attribution.tsv", cts_power_attribution)
    correlations: list[dict[str, object]] = []
    predictors = (
        "delta_distance_to_original_centroid_um",
        "delta_nearest_centroid_distance_um",
        "delta_nearest_centroid_margin_um",
        "delta_nearest_assignment_load_variance_ff2",
        "delta_nearest_assignment_max_load_ff",
        "delta_nearest_assignment_radius2_um2",
        "nearest_branch_changed",
        "actual_branch_changed",
        "actual_branch_membership_churn_sinks",
        "actual_cts_load_variance_ff2",
        "delta_actual_cts_load_variance_ff2",
        "dummy_load_master_change_count",
    )
    targets = (
        "routed_idle_clock_internal_delta_nw",
        "routed_typical_clock_internal_delta_nw",
        "routed_peak_clock_internal_delta_nw",
        "routed_idle_dynamic_delta_nw",
        "routed_typical_dynamic_delta_nw",
        "routed_peak_dynamic_delta_nw",
    )
    for predictor in predictors:
        for target in targets:
            x = [float(row[predictor]) for row in features]
            y = [float(row[target]) for row in features]
            correlations.append({
                "predictor": predictor,
                "target": target,
                "candidate_count": len(features),
                "unique_moved_sinks": len({row["instance"] for row in features}),
                "spearman_rho": spearman(x, y),
                "kendall_tau_b": kendall_tau_b(x, y),
            })
    write_tsv(output_dir / "rank_correlations.tsv", correlations)

    branch_counts = {}
    for row in features:
        branch_counts[row["actual_cts_branch"]] = branch_counts.get(row["actual_cts_branch"], 0) + 1
    moved_count_set = sorted({int(row["moved_sink_count_in_placement_odb"]) for row in features})
    changed = [row["candidate_id"] for row in features if int(row["actual_branch_changed"])]
    dummy_changed = [
        row for row in features if int(row["dummy_load_master_change_count"])
    ]
    rand023_feature = next((row for row in features if row["candidate_id"] == "rand023"), None)
    rand023_power = next((row for row in cts_power_attribution if row["candidate_id"] == "rand023"), None)
    rand023_branch3 = next(
        row for row in dummy_balance_rows
        if row["candidate_id"] == "rand023" and row["branch"] == "clknet_2_3__leaf_clk"
    )
    baseline_branch3 = next(
        row for row in dummy_balance_rows
        if row["candidate_id"] == "baseline" and row["branch"] == "clknet_2_3__leaf_clk"
    )
    summary = [
        "# GCD baseline-CTS centroid proxy analysis",
        "",
        f"Analyzed {len(features)} frozen one-flop relocation candidates covering "
        f"{len({row['instance'] for row in features})} unique sinks.",
        f"Baseline CTS has {len(base_branches)} leaf branches and {len(base_cts)} register clock sinks.",
        f"Placement ODB moved-sink-count values: {moved_count_set}.",
        f"Candidates whose moved flop changed its actual CTS leaf branch: {', '.join(changed) if changed else 'none'}.",
        "Candidates whose CTS dummy-load master changed: "
        + (
            ", ".join(f"{row['candidate_id']} ({row['dummy_load_master_changes']})" for row in dummy_changed)
            if dummy_changed else "none"
        )
        + ".",
        f"Nearest-centroid assignments changed for {sum(int(row['nearest_branch_changed']) for row in features)}/{len(features)} candidates.",
        f"For rand023, the moved sink stayed nearest to its baseline centroid but CTS changed its branch; CTS reassigned {rand023_feature['actual_branch_membership_churn_sinks']} sinks total.",
        f"rand023 actual CTS sink-load variance changed by {float(rand023_feature['delta_actual_cts_load_variance_ff2']):+.3f} fF².",
        f"For rand023, clock-buffer internal power changed by {float(rand023_power['cts_buffer_internal_delta_nw']):+.2f} nW and dummy-load internal power by {float(rand023_power['cts_dummy_load_internal_delta_nw']):+.2f} nW.",
        f"Source-based sibling-cap model: branch 3's dummy deficit moved from {float(baseline_branch3['dummy_cap_deficit_ff']):.3f} fF in baseline to {float(rand023_branch3['dummy_cap_deficit_ff']):.3f} fF in rand023; the nearest among the observed dummy masters changed from {baseline_branch3['nearest_among_observed_masters']} to {rand023_branch3['nearest_among_observed_masters']}.",
        f"Measured input caps were {dummy_caps_ff['sky130_fd_sc_hd__clkbuf_8']:.3f} fF (`clkbuf_8`) and {dummy_caps_ff['sky130_fd_sc_hd__clkinv_2']:.3f} fF (`clkinv_2`). The branch-3 deficit crossed their nearest-cell midpoint of {(dummy_caps_ff['sky130_fd_sc_hd__clkbuf_8'] + dummy_caps_ff['sky130_fd_sc_hd__clkinv_2']) / 2.0:.3f} fF.",
        f"Actual branch assignment frequencies for the moved sink: {branch_counts}.",
        "",
        "This is exploratory evaluation of a proxy chosen after inspecting this candidate pool; use a newly frozen pool for an unbiased confirmation.",
        "The centroid model uses baseline post-CTS branch centroids as a reference. It does not reproduce TritonCTS's cap-aware sink matching, H-tree partitioning, dummy-load selection, slew propagation, or Liberty internal-power calculation.",
        "TritonCTS dummy loads are physical clock cells inserted to balance buffer output capacitance. Their selected master must be included when explaining the routed clock-group internal-power delta.",
        "The source-faithful balancing rule is sibling-based: each child buffer's output capacitance is the sum of its direct sink pin capacitances (wire capacitance is explicitly not included here), the largest child output capacitance is the target, and a dummy is selected to minimize the absolute difference between its Liberty input capacitance and the child's deficit. `sibling_dummy_deficits.tsv` compares these calculated deficits with observed dummy cells. The placement-only proxy still needs to predict CTS branch membership before it can use this rule; these rows use post-CTS membership and are therefore diagnostic, not an independent predictor.",
        "The phrase 'downsizing' is misleading if read as lower input capacitance: in this library, `clkinv_2` has a larger input capacitance than `clkbuf_8`, while its measured internal power under this workload is much lower.",
        "",
        "See `baseline_cts_clusters.tsv`, `candidate_branch_features.tsv`, `sibling_dummy_deficits.tsv`, `dummy_cell_input_caps.tsv`, `candidate_cts_instance_power.tsv`, `clock_cell_power_attribution.tsv`, and `rank_correlations.tsv` for branch loads, candidate features, source-based dummy deficits, Liberty caps, CTS cell power, attribution, and rank statistics.",
        "",
        "## Rank correlations",
        "",
        "Positive correlation means that a larger proxy delta follows a larger routed-power delta.",
        "",
    ]
    for row in correlations:
        if row["target"] in {"routed_idle_clock_internal_delta_nw", "routed_peak_clock_internal_delta_nw", "routed_peak_dynamic_delta_nw"}:
            summary.append(
                f"- `{row['predictor']}` vs `{row['target']}`: "
                f"Spearman ρ={float(row['spearman_rho']):.3f}, "
                f"Kendall τb={float(row['kendall_tau_b']):.3f}."
            )
    (output_dir / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"Analyzed {len(features)} candidates; results: {output_dir}")


if __name__ == "__main__":
    main()
