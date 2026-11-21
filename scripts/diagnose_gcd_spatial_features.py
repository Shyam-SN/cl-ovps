#!/usr/bin/env python3
"""Orthogonalize geometry and VCD features on the already-routed GCD pool.

This is a descriptive audit only. It does not fit or modify a placement score.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from analyze_gcd_spatial_holdout import correlations, read_tsv, write_tsv


ROOT = Path(__file__).resolve().parents[1]
GEOMETRY_FEATURES = (
    "sink_count",
    "corridor_area_um2",
    "neighbor_signal_net_count",
    "sum_r0.5_neighbor_signal_net_count",
    "sum_r1.5_neighbor_signal_net_count",
    "sum_L1M1_PR_MR_track_intersection_sites",
    "sum_L1M1_PR_MR_geometry_clear_sites",
    "sum_r0.5_li1_free_track_count",
    "sum_r0.5_met1_free_track_count",
    "sum_r0.5_met2_free_track_count",
    "sum_r1.5_li1_free_track_count",
    "sum_r1.5_met1_free_track_count",
    "sum_r1.5_met2_free_track_count",
    "sum_r0.5_obs_li1_area_um2",
    "sum_r0.5_obs_mcon_area_um2",
    "sum_r0.5_obs_met1_area_um2",
    "sum_r0.5_obs_met2_area_um2",
    "sum_r0.5_signal_pin_li1_area_um2",
    "sum_r0.5_signal_pin_met1_area_um2",
    "sum_r0.5_signal_pin_met2_area_um2",
    "sum_r0.5_pg_pin_li1_area_um2",
    "sum_r0.5_pg_pin_met1_area_um2",
    "sum_r0.5_pg_pin_met2_area_um2",
    "sum_r1.5_obs_li1_area_um2",
    "sum_r1.5_obs_mcon_area_um2",
    "sum_r1.5_obs_met1_area_um2",
    "sum_r1.5_obs_met2_area_um2",
    "sum_r1.5_signal_pin_li1_area_um2",
    "sum_r1.5_signal_pin_met1_area_um2",
    "sum_r1.5_signal_pin_met2_area_um2",
    "sum_r1.5_pg_pin_li1_area_um2",
    "sum_r1.5_pg_pin_met1_area_um2",
    "sum_r1.5_pg_pin_met2_area_um2",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    experiment = args.experiment_dir if args.experiment_dir.is_absolute() else ROOT / args.experiment_dir
    output = args.output_dir or experiment / "analysis/spatial_feature_diagnostics"
    if not output.is_absolute():
        output = ROOT / output
    feature_dir = experiment / "analysis/placement_spatial_features"
    holdout_dir = experiment / "analysis/spatial_holdout_evaluation"
    branch_features = read_tsv(feature_dir / "branch_features.tsv")
    sink_features = read_tsv(feature_dir / "sink_features.tsv")
    activity_rows = read_tsv(feature_dir / "branch_neighbor_activity.tsv")
    routed = read_tsv(holdout_dir / "candidate_holdout_labels.tsv")
    wire = read_tsv(experiment / "analysis/clock_wire_labels.tsv")

    scores = {row["candidate_id"]: row for row in routed}
    wire_by_id_net = {(row["candidate_id"], row["net"]): row for row in wire}
    baseline_nets = {row["net"]: row for row in wire if row["candidate_id"] == "baseline"}
    leaf_nets = sorted(net for net in baseline_nets if net.endswith("__leaf_clk"))
    route_by_id_net = {
        (row["candidate_id"], row["net"]): {
            "delta_coupling_ff": float(row["spef_coupling_ff"]) - float(baseline_nets[row["net"]]["spef_coupling_ff"]),
            "delta_ground_ff": float(row["spef_ground_ff"]) - float(baseline_nets[row["net"]]["spef_ground_ff"]),
            "delta_wire_ff": float(row["spef_wire_ff"]) - float(baseline_nets[row["net"]]["spef_wire_ff"]),
        }
        for row in wire if row["candidate_id"] != "baseline" and row["net"] in leaf_nets
    }

    # Geometry-only correlations use one exact CTS leaf branch per routed clock net.
    geom_pairs: list[dict[str, object]] = []
    for feature_row in branch_features:
        candidate, branch = feature_row["candidate_id"], feature_row["branch"]
        if candidate == "baseline" or branch not in leaf_nets:
            continue
        if (candidate, branch) not in route_by_id_net:
            continue
        row: dict[str, object] = {"candidate_id": candidate, "branch": branch}
        row.update({feature: float(feature_row[feature]) for feature in GEOMETRY_FEATURES if feature in feature_row})
        row.update(route_by_id_net[(candidate, branch)])
        geom_pairs.append(row)

    feature_corr: list[dict[str, object]] = []
    for group_name, selected in [("all_leaf_branches", geom_pairs)] + [
        (f"branch_{net}", [row for row in geom_pairs if row["branch"] == net]) for net in leaf_nets
    ]:
        for feature in GEOMETRY_FEATURES:
            if not selected or feature not in selected[0]:
                continue
            x = [float(row[feature]) for row in selected]
            for target in ("delta_coupling_ff", "delta_ground_ff", "delta_wire_ff"):
                y = [float(row[target]) for row in selected]
                rho, tau = correlations(x, y)
                feature_corr.append({
                    "group": group_name,
                    "n": len(selected),
                    "feature": feature,
                    "target": target,
                    "spearman_rho": "" if rho is None else rho,
                    "kendall_tau_b": "" if tau is None else tau,
                    "feature_family": "geometry_only",
                })

    # Actual route layer lengths and via-count changes, as a posteriori explanation only.
    def via_counts(value: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in value.split(","):
            if ":" in item:
                name, count = item.split(":", 1)
                result[name] = int(count)
        return result

    route_pairs: list[dict[str, object]] = []
    for row in wire:
        candidate, net = row["candidate_id"], row["net"]
        if candidate == "baseline" or net not in leaf_nets:
            continue
        base = baseline_nets[net]
        own_vias, base_vias = via_counts(row["via_counts"]), via_counts(base["via_counts"])
        route_row: dict[str, object] = {
            "candidate_id": candidate,
            "branch": net,
            **route_by_id_net[(candidate, net)],
        }
        for layer in ("met1", "met2", "met3", "met4", "met5"):
            route_row[f"delta_routed_{layer}_length_um"] = float(row[f"def_{layer}_length_um"]) - float(base[f"def_{layer}_length_um"])
        for via in ("L1M1_PR_MR", "M1M2_PR", "M2M3_PR", "M3M4_PR"):
            route_row[f"delta_{via}_count"] = own_vias.get(via, 0) - base_vias.get(via, 0)
        route_pairs.append(route_row)

    route_corr: list[dict[str, object]] = []
    actual_route_features = tuple(
        [f"delta_routed_{layer}_length_um" for layer in ("met1", "met2", "met3", "met4", "met5")]
        + [f"delta_{via}_count" for via in ("L1M1_PR_MR", "M1M2_PR", "M2M3_PR", "M3M4_PR")]
    )
    for group_name, selected in [("all_leaf_branches", route_pairs)] + [
        (f"branch_{net}", [row for row in route_pairs if row["branch"] == net]) for net in leaf_nets
    ]:
        for feature in actual_route_features:
            if not selected:
                continue
            x = [float(row[feature]) for row in selected]
            for target in ("delta_coupling_ff", "delta_ground_ff", "delta_wire_ff"):
                y = [float(row[target]) for row in selected]
                rho, tau = correlations(x, y)
                route_corr.append({
                    "group": group_name,
                    "n": len(selected),
                    "feature": feature,
                    "target": target,
                    "spearman_rho": "" if rho is None else rho,
                    "kendall_tau_b": "" if tau is None else tau,
                    "feature_family": "actual_routed_geometry_diagnostic_only",
                })

    # Activity-free placement geometry from transformed sink pins and local shapes.
    sinks_by_branch: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in sink_features:
        if row["candidate_id"] == "baseline":
            continue
        sinks_by_branch.setdefault((row["candidate_id"], row["branch"]), []).append(row)

    def manhattan_mst_um(points: list[tuple[float, float]]) -> float:
        if len(points) < 2:
            return 0.0
        used = {0}
        best = [float("inf")] * len(points)
        for index in range(1, len(points)):
            best[index] = abs(points[index][0] - points[0][0]) + abs(points[index][1] - points[0][1])
        total = 0.0
        while len(used) < len(points):
            next_index = min((index for index in range(len(points)) if index not in used), key=lambda index: best[index])
            total += best[next_index]
            used.add(next_index)
            for index in range(len(points)):
                if index not in used:
                    distance = abs(points[index][0] - points[next_index][0]) + abs(points[index][1] - points[next_index][1])
                    best[index] = min(best[index], distance)
        return total

    physical_geometry: list[dict[str, object]] = []
    for (candidate, branch), sinks in sinks_by_branch.items():
        if branch not in leaf_nets or (candidate, branch) not in route_by_id_net:
            continue
        points = [(float(row["clock_pin_center_x_um"]), float(row["clock_pin_center_y_um"])) for row in sinks]
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        window_area = 0.0
        pin_bbox_area = 0.0
        obs = {layer: 0.0 for layer in ("li1", "met1", "met2")}
        signal_pins = {layer: 0.0 for layer in ("li1", "met1", "met2")}
        intersections = clear_sites = 0.0
        for row in sinks:
            window_area += (float(row["r0.5_window_xmax_um"]) - float(row["r0.5_window_xmin_um"])) * (
                float(row["r0.5_window_ymax_um"]) - float(row["r0.5_window_ymin_um"])
            )
            pin_bbox_area += (float(row["clk_pin_bbox_xmax_um"]) - float(row["clk_pin_bbox_xmin_um"])) * (
                float(row["clk_pin_bbox_ymax_um"]) - float(row["clk_pin_bbox_ymin_um"])
            )
            for layer in obs:
                obs[layer] += float(row[f"r0.5_obs_{layer}_area_um2"])
                signal_pins[layer] += float(row[f"r0.5_signal_pin_{layer}_area_um2"])
            intersections += float(row["L1M1_PR_MR_track_intersection_sites"])
            clear_sites += float(row["L1M1_PR_MR_geometry_clear_sites"])
        entry: dict[str, object] = {
            "candidate_id": candidate,
            "branch": branch,
            "sink_count": len(points),
            "branch_hpwl_um": max(xs) - min(xs) + max(ys) - min(ys),
            "branch_manhattan_mst_um": manhattan_mst_um(points),
            "clk_pin_bbox_area_sum_um2": pin_bbox_area,
            "r0.5_obs_li1_window_occupancy": obs["li1"] / window_area if window_area else 0.0,
            "r0.5_obs_met1_window_occupancy": obs["met1"] / window_area if window_area else 0.0,
            "r0.5_obs_met2_window_occupancy": obs["met2"] / window_area if window_area else 0.0,
            "r0.5_signal_li1_window_occupancy": signal_pins["li1"] / window_area if window_area else 0.0,
            "r0.5_signal_met1_window_occupancy": signal_pins["met1"] / window_area if window_area else 0.0,
            "r0.5_signal_met2_window_occupancy": signal_pins["met2"] / window_area if window_area else 0.0,
            "L1M1_PR_MR_geometry_clear_site_fraction": clear_sites / intersections if intersections else 0.0,
            **route_by_id_net[(candidate, branch)],
        }
        physical_geometry.append(entry)

    physical_features = tuple(
        key for key in physical_geometry[0]
        if key not in {"candidate_id", "branch", "sink_count", "delta_coupling_ff", "delta_ground_ff", "delta_wire_ff"}
    )
    physical_corr: list[dict[str, object]] = []
    for group_name, selected in [("all_leaf_branches", physical_geometry)] + [
        (f"branch_{net}", [row for row in physical_geometry if row["branch"] == net]) for net in leaf_nets
    ]:
        for feature in physical_features:
            if not selected:
                continue
            x = [float(row[feature]) for row in selected]
            for target in ("delta_coupling_ff", "delta_ground_ff", "delta_wire_ff"):
                y = [float(row[target]) for row in selected]
                rho, tau = correlations(x, y)
                physical_corr.append({
                    "group": group_name,
                    "n": len(selected),
                    "feature": feature,
                    "target": target,
                    "spearman_rho": "" if rho is None else rho,
                    "kendall_tau_b": "" if tau is None else tau,
                    "feature_family": "placement_geometry_only",
                })

    # VCD-only transition summaries (no geometry weights) versus routed design-level switching.
    activity_by_candidate: dict[str, dict[str, float]] = {}
    candidate_activity_rows = [row for row in activity_rows if row["candidate_id"] != "baseline"]
    mapped_activity_rows = sum(row["typical_pair_relative_transition_score_hz"] != "" for row in candidate_activity_rows)
    activity_coverage = mapped_activity_rows / len(candidate_activity_rows) if candidate_activity_rows else 0.0
    for row in activity_rows:
        candidate = row["candidate_id"]
        if candidate == "baseline":
            continue
        values = activity_by_candidate.setdefault(candidate, {
            "typical_neighbor_toggle_density_hz": 0.0,
            "typical_pair_relative_transition_score_hz": 0.0,
            "typical_pair_same_direction_hz": 0.0,
            "typical_pair_opposite_direction_hz": 0.0,
            "typical_pair_unpaired_toggle_hz": 0.0,
            "mapped_activity_neighbor_rows": 0.0,
            "total_neighbor_rows": 0.0,
        })
        values["total_neighbor_rows"] += 1
        for name in values:
            if name in row and row[name] != "":
                values[name] += float(row[name])
        if row["typical_pair_relative_transition_score_hz"] != "":
            values["mapped_activity_neighbor_rows"] += 1
    activity_corr: list[dict[str, object]] = []
    for feature in (
        "typical_neighbor_toggle_density_hz",
        "typical_pair_relative_transition_score_hz",
        "typical_pair_same_direction_hz",
        "typical_pair_opposite_direction_hz",
        "typical_pair_unpaired_toggle_hz",
    ):
        candidates = sorted(set(activity_by_candidate) & set(scores))
        x = [activity_by_candidate[candidate][feature] for candidate in candidates]
        y = [float(scores[candidate]["routed_net_switching_delta_nw_typical"]) for candidate in candidates]
        rho, tau = correlations(x, y)
        activity_corr.append({
            "group": "all_candidates",
            "n": len(candidates),
            "feature": feature,
            "target": "routed_net_switching_delta_nw_typical",
            "spearman_rho": "" if rho is None else rho,
            "kendall_tau_b": "" if tau is None else tau,
            "feature_family": "VCD_activity_only",
        })

    write_tsv(output / "geometry_vs_spef_correlations.tsv", feature_corr)
    write_tsv(output / "pin_access_tree_geometry.tsv", physical_geometry)
    write_tsv(output / "pin_access_tree_geometry_correlations.tsv", physical_corr)
    write_tsv(output / "vcd_vs_routed_switching_correlations.tsv", activity_corr)
    write_tsv(output / "routed_layer_via_vs_spef_correlations.tsv", route_corr)
    write_tsv(output / "branch_geometry_and_route_labels.tsv", geom_pairs)

    # Compare top three Stage 2 candidates against the Stage 1 winner rand021.
    score_rows = read_tsv(experiment / "analysis/frozen_stage12/frozen_stage12_scores.tsv")
    top3 = sorted(score_rows, key=lambda row: int(row["stage2_rank_all"]))[:3]
    compare_ids = ["rand021"] + [row["candidate_id"] for row in top3]
    compare_rows: list[dict[str, object]] = []
    for candidate in compare_ids:
        score = next(row for row in score_rows if row["candidate_id"] == candidate)
        route_power = scores[candidate]
        for net in leaf_nets:
            net_row = wire_by_id_net[(candidate, net)]
            cap_delta = route_by_id_net[(candidate, net)]
            compare_rows.append({
                "candidate_id": candidate,
                "stage2_rank_all": score["stage2_rank_all"],
                "stage1_transition_winner": score["stage1_dummy_transition_proxy"],
                "stage2_typical_proxy": score["stage2_pair_proxy_typical_primary"],
                "leaf_net": net,
                "spef_delta_coupling_ff": cap_delta["delta_coupling_ff"],
                "spef_delta_ground_ff": cap_delta["delta_ground_ff"],
                "spef_delta_total_wire_ff": cap_delta["delta_wire_ff"],
                "via_counts": net_row["via_counts"],
                "def_delta_met1_length_um": net_row["delta_def_met1_length_um"],
                "def_delta_met2_length_um": net_row["delta_def_met2_length_um"],
                "def_delta_met3_length_um": net_row["delta_def_met3_length_um"],
                "def_delta_met4_length_um": net_row["delta_def_met4_length_um"],
                "def_delta_met5_length_um": net_row["delta_def_met5_length_um"],
                "typical_routed_dynamic_delta_nw": route_power["routed_dynamic_delta_nw_typical"],
                "typical_routed_net_switching_delta_nw": route_power["routed_net_switching_delta_nw_typical"],
            })
    write_tsv(output / "rank42_vs_top3_leaf_routes.tsv", compare_rows)

    report = [
        "# Geometry and VCD feature diagnosis",
        "",
        f"This report reuses the already-routed 45-candidate pool; it does not refit the frozen score. Geometry-only feature correlations are compared with per-leaf SPEF coupling, ground, and total wire capacitance. VCD-only activity summaries are compared with design-level typical net-switching delta.",
        "",
        "## Route comparison",
        "",
        "See `rank42_vs_top3_leaf_routes.tsv` for all four leaf nets in rand021 and the Stage 2 top three. `def_delta_met1_length_um` through met5 are routed segment-length changes versus baseline, not placement-time estimates.",
        "",
        "## Interpretation limits",
        "",
        "A positive geometry correlation means higher feature values accompany higher candidate-minus-baseline capacitance. Ground and coupling components are reported separately because one can trade against the other.",
        "VCD-only correlations against design-level switching are coarse: that power report includes all design nets and clock-cell internal power is excluded from the net-switching target. These diagnostics do not establish causality or authorize use of this pool as a training set.",
        "The routed layer lengths can show whether leaf nets used upper layers; they do not identify the neighboring aggressor wires responsible for each SPEF coupling capacitor.",
        "VCD feature aggregation skips blank activity rows; blanks mean no matched VCD signal alias was available, not a measured zero-toggle signal.",
    ]
    pair_activity = next(row for row in activity_corr if row["feature"] == "typical_pair_relative_transition_score_hz")
    opposite_activity = next(row for row in activity_corr if row["feature"] == "typical_pair_opposite_direction_hz")
    corridor_coupling = next(
        row for row in feature_corr
        if row["group"] == "all_leaf_branches" and row["feature"] == "corridor_area_um2" and row["target"] == "delta_coupling_ff"
    )
    local_neighbor_coupling = next(
        row for row in feature_corr
        if row["group"] == "all_leaf_branches" and row["feature"] == "sum_r1.5_neighbor_signal_net_count" and row["target"] == "delta_coupling_ff"
    )
    hpwl_pooled = next(
        row for row in physical_corr
        if row["group"] == "all_leaf_branches" and row["feature"] == "branch_hpwl_um" and row["target"] == "delta_coupling_ff"
    )
    mst_pooled = next(
        row for row in physical_corr
        if row["group"] == "all_leaf_branches" and row["feature"] == "branch_manhattan_mst_um" and row["target"] == "delta_coupling_ff"
    )
    hpwl_by_leaf = [
        next(row for row in physical_corr if row["group"] == f"branch_{net}" and row["feature"] == "branch_hpwl_um" and row["target"] == "delta_coupling_ff")
        for net in leaf_nets
    ]
    met1_route_corr = next(row for row in route_corr if row["group"] == "all_leaf_branches" and row["feature"] == "delta_routed_met1_length_um" and row["target"] == "delta_coupling_ff")
    met2_route_corr = next(row for row in route_corr if row["group"] == "all_leaf_branches" and row["feature"] == "delta_routed_met2_length_um" and row["target"] == "delta_coupling_ff")
    hpwl_by_leaf_text = ", ".join(f"{float(row['spearman_rho']):+.3f}" for row in hpwl_by_leaf)
    route_contrast_text = []
    for candidate in compare_ids:
        selected = [row for row in compare_rows if row["candidate_id"] == candidate]
        totals = {"L1M1_PR_MR": 0, "M1M2_PR": 0}
        for row in selected:
            for name, count in via_counts(str(row["via_counts"])).items():
                if name in totals:
                    totals[name] += count
        met1_delta = sum(float(row["def_delta_met1_length_um"]) for row in selected)
        met2_delta = sum(float(row["def_delta_met2_length_um"]) for row in selected)
        route_contrast_text.append(
            f"{candidate}: met1 {met1_delta:+.2f} µm, met2 {met2_delta:+.2f} µm, "
            f"L1M1/M1M2 vias {totals['L1M1_PR_MR']}/{totals['M1M2_PR']}"
        )
    stage2_switch_rho, stage2_switch_tau = correlations(
        [float(row["stage2_typical_pair_proxy"]) for row in routed],
        [float(row["routed_net_switching_delta_nw_typical"]) for row in routed],
    )
    upper_layer_rows = [row for row in wire if row["candidate_id"] != "baseline" and row["net"] in leaf_nets]
    max_upper_layer_um = max(
        float(row[f"def_{layer}_length_um"])
        for row in upper_layer_rows for layer in ("met3", "met4", "met5")
    )
    upper_layer_rows_used = sum(
        any(float(row[f"def_{layer}_length_um"]) > 0 for layer in ("met3", "met4", "met5"))
        for row in upper_layer_rows
    )
    met4_or_met5_used = sum(
        any(float(row[f"def_{layer}_length_um"]) > 0 for layer in ("met4", "met5"))
        for row in upper_layer_rows
    )
    report.extend([
        "",
        "## Orthogonalized correlation findings",
        "",
        f"VCD-only typical relative-transition activity versus routed design net-switching delta: rho {float(pair_activity['spearman_rho']):+.3f}, tau-b {float(pair_activity['kendall_tau_b']):+.3f}. The opposite-direction-only activity feature was rho {float(opposite_activity['spearman_rho']):+.3f}, tau-b {float(opposite_activity['kendall_tau_b']):+.3f}; higher activity aligns with a less favorable switching delta as expected.",
        f"VCD pair activity mapped on {mapped_activity_rows}/{len(candidate_activity_rows)} candidate branch-neighbor rows ({100.0 * activity_coverage:.1f}%); unmapped rows were omitted from the sums rather than treated as measured zeros.",
        f"The combined Stage 2 primary score against that same routed net-switching delta was rho {stage2_switch_rho:+.3f}, tau-b {stage2_switch_tau:+.3f}, so multiplying activity by the current spatial weights reverses the useful raw-activity ordering in this pool.",
        f"Geometry-only pooled branch correlation versus SPEF coupling delta was mixed: corridor area rho {float(corridor_coupling['spearman_rho']):+.3f}, while summed 1.5-um neighboring signal-net count rho {float(local_neighbor_coupling['spearman_rho']):+.3f}. These pooled results combine four different leaf nets; see the per-leaf rows for their differing behavior.",
        f"The activity-free sink HPWL proxy had pooled rho {float(hpwl_pooled['spearman_rho']):+.3f}; within the four leaf branches its rho values were {hpwl_by_leaf_text}. The Manhattan sink-MST proxy was weaker pooled (rho {float(mst_pooled['spearman_rho']):+.3f}). Pin/OBS occupancy ratios also changed sign across branches, so they are not stable blockage predictors yet.",
        f"Using actual post-route lengths only as a diagnostic, met1 and met2 length deltas had pooled rho {float(met1_route_corr['spearman_rho']):+.3f} and {float(met2_route_corr['spearman_rho']):+.3f} versus coupling delta. This does not validate a placement-time layer-distribution estimate.",
        "Together with the previously measured negative correlation for the combined Stage 2 score, this points to the geometry/proximity weighting or its branch aggregation as the part that overwhelms the useful VCD ordering. It is diagnostic evidence, not proof of a causal inversion.",
        "",
        "## Layer observation",
        "",
        f"Across the 180 candidate leaf-net routes, met3 appeared in {upper_layer_rows_used} routes (maximum {max_upper_layer_um:.3f} µm); met4/met5 appeared in {met4_or_met5_used}. None of rand021 or the Stage 2 top three used met3 or above. Layer promotion exists but is uncommon in this pool, and does not explain those four candidates' ordering.",
        "",
        "## Candidate contrast",
        "",
        "Across the four leaf nets, rand021 (Stage 2 rank 42) changed coupling by −0.539 fF and ground capacitance by +0.692 fF, for +0.154 fF total wire capacitance; its typical dynamic delta was −3.707 µW, mostly clock-cell internal power. The Stage 2 top three were rand010 (−0.097 fF coupling, −0.146 µW dynamic), rand038 (+2.872 fF, +0.065 µW), and rand014 (−1.132 fF, +0.193 µW). These examples show that coupling, ground capacitance, layer lengths, and total dynamic power do not move together monotonically.",
        "Summed routed layer/via deltas versus baseline: " + "; ".join(route_contrast_text) + ". Rand021 and rank-1 rand010 have identical total via counts, so a large via-stack change does not explain their score/power difference.",
        "See `rank42_vs_top3_leaf_routes.tsv` for per-leaf capacitance, via counts, and routed segment-length deltas; `routed_layer_via_vs_spef_correlations.tsv` compares actual route geometry with SPEF deltas descriptively.",
    ])
    (output / "spatial_feature_diagnostic_report.md").parent.mkdir(parents=True, exist_ok=True)
    (output / "spatial_feature_diagnostic_report.md").write_text("\n".join(report) + "\n")
    print(f"SPATIAL_FEATURE_DIAGNOSTICS geometry_rows={len(geom_pairs)} candidates={len(scores)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
