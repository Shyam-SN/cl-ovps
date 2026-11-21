#!/usr/bin/env python3
"""Score a fresh GCD placement pool with the frozen routed-cap ridge model."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path

VOLTAGE = 1.8
POWER_FACTOR = 0.5 * VOLTAGE**2
WORKLOADS = ("idle", "typical", "peak")


def read_table(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def write_table(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_caps(path: Path) -> dict[str, dict[str, str | float]]:
    result = {}
    with path.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) != 6 or fields[1] == "pin":
                continue
            _, pin, net, pin_cap, wire_cap, total_cap = fields
            result[pin] = {"net": net, "pin_cap": float(pin_cap),
                           "wire_cap": float(wire_cap), "total_cap": float(total_cap)}
    return result


def read_geometry(path: Path) -> dict[str, dict[str, float]]:
    result = {}
    for row in read_table(path):
        if row["sig_type"] != "SIGNAL":
            continue
        dbu = float(row["dbu_per_micron"])
        result[row["net"]] = {
            "fanout": float(row["fanout"]),
            "endpoint_count": float(row["endpoint_count"]),
            "hpwl_um": float(row["hpwl_dbu"]) / dbu,
            "bbox_width_um": float(row["bbox_width_dbu"]) / dbu,
            "bbox_height_um": float(row["bbox_height_dbu"]) / dbu,
        }
    return result


def read_densities(path: Path) -> dict[str, float]:
    return {row["net"]: float(row["density_hz"]) for row in read_table(path, "\t")}


def read_audit(path: Path) -> dict[str, dict[str, str]]:
    return {row["pair_id"]: row for row in read_table(path, "\t")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--trial-dir", type=Path,
                        default=Path("build/power/gcd_prospective_rank_20261027"))
    parser.add_argument("--model", type=Path,
                        default=Path("build/power/gcd_cross_design_score_20261027/trained_ridge_cap_geometry.json"))
    parser.add_argument("--top-count", type=int, default=3)
    args = parser.parse_args()
    root = args.root.resolve()
    trial = (root / args.trial_dir).resolve() if not args.trial_dir.is_absolute() else args.trial_dir
    model_path = (root / args.model).resolve() if not args.model.is_absolute() else args.model

    model = json.loads(model_path.read_text())
    terms = model["terms"]
    mean = model["weighted_feature_mean"]
    scale = model["weighted_feature_scale"]
    coef = model["standardized_coefficients"]
    intercept = float(model["intercept_ff"])
    baseline_caps = read_caps(root / "build/power/gcd_baseline20ns/typical.driver_caps.tsv")
    baseline_geom = read_geometry(root / "build/power/gcd_cross_design_score_20261027/physical/gcd_baseline20ns.nets.csv")
    baseline_nets = {str(row["net"]) for row in baseline_caps.values()}
    candidates = read_table(trial / "candidate_pool.tsv", "\t")
    audits = {workload: read_audit(trial / f"audit_{workload}.tsv") for workload in WORKLOADS}
    densities = {workload: read_densities(root / f"build/power/gcd_baseline20ns/{workload}.active_net_density.tsv")
                 for workload in WORKLOADS}
    candidate_rows = []
    net_rows = []
    for candidate in candidates:
        pair_id = candidate["pair_id"]
        candidate_odb = trial / "candidate_odb" / f"{pair_id}.odb"
        if not candidate_odb.is_file():
            raise FileNotFoundError(candidate_odb)
        audit_rows = [audits[w].get(pair_id) for w in WORKLOADS]
        if any(row is None for row in audit_rows):
            raise ValueError(f"missing one or more workload audits for {pair_id}")
        if any(row["status"] != "evaluated" or row["timing_ok"] != "1" for row in audit_rows):
            raise ValueError(f"candidate failed placement legality/timing screening: {pair_id}")

        candidate_caps = read_caps(trial / "placement_caps" / pair_id / "typical.driver_caps.tsv")
        candidate_geom = read_geometry(trial / "physical" / f"{pair_id}.nets.csv")
        if len(baseline_nets) != 314 or not baseline_nets.issubset(candidate_geom):
            raise ValueError(f"expected all 314 baseline signal nets in {pair_id} geometry")
        features_by_net = {}
        for pin, base_cap in baseline_caps.items():
            net = str(base_cap["net"])
            if net not in baseline_nets:
                continue
            cand_cap = candidate_caps.get(pin)
            if cand_cap is None or cand_cap["net"] != net:
                raise ValueError(f"candidate driver-cap join failed for {pair_id}/{pin}/{net}")
            base = baseline_geom[net]
            cand = candidate_geom[net]
            features = {
                "delta_place_wire_cap_ff": (float(cand_cap["wire_cap"]) - float(base_cap["wire_cap"])) * 1e15,
                "delta_place_load_cap_ff": (float(cand_cap["total_cap"]) - float(base_cap["total_cap"])) * 1e15,
                "place_delta_hpwl_um": cand["hpwl_um"] - base["hpwl_um"],
                "place_abs_delta_hpwl_um": abs(cand["hpwl_um"] - base["hpwl_um"]),
                "place_delta_bbox_width_um": cand["bbox_width_um"] - base["bbox_width_um"],
                "place_delta_bbox_height_um": cand["bbox_height_um"] - base["bbox_height_um"],
                "place_baseline_fanout": base["fanout"],
                "place_delta_fanout": cand["fanout"] - base["fanout"],
                "place_delta_endpoint_count": cand["endpoint_count"] - base["endpoint_count"],
                "place_baseline_hpwl_um": base["hpwl_um"],
                "place_baseline_endpoint_count": base["endpoint_count"],
            }
            pred_cap_ff = intercept + sum(
                ((features[term] - float(mean[i])) / float(scale[i])) * float(coef[i])
                for i, term in enumerate(terms)
            )
            features_by_net[net] = (features, pred_cap_ff)
            for workload in WORKLOADS:
                density = densities[workload].get(net)
                if density is None:
                    raise ValueError(f"missing {workload} VCD density for {net}")
                net_rows.append({"pair_id": pair_id, "net": net, "workload": workload,
                                 "predicted_routed_delta_wire_cap_ff": pred_cap_ff,
                                 "baseline_density_hz": density,
                                 "predicted_signal_power_delta_nw": POWER_FACTOR * density * pred_cap_ff * 1e-6})

        predicted_by_workload = {}
        for workload in WORKLOADS:
            predicted_by_workload[workload] = sum(
                POWER_FACTOR * densities[workload][net] * pred_cap_ff * 1e-15
                for net, (_, pred_cap_ff) in features_by_net.items()
            ) * 1e9
        worst = max(predicted_by_workload.values())
        average = statistics.mean(predicted_by_workload.values())
        candidate_rows.append({
            "pair_id": pair_id,
            "high_instance": candidate["high_instance"],
            "low_instance": candidate["low_instance"],
            "master": candidate["master"],
            "predicted_idle_signal_delta_nw": predicted_by_workload["idle"],
            "predicted_typical_signal_delta_nw": predicted_by_workload["typical"],
            "predicted_peak_signal_delta_nw": predicted_by_workload["peak"],
            "predicted_worst_workload_delta_nw": worst,
            "predicted_mean_workload_delta_nw": average,
            "placement_idle_delta_nw": float(audits["idle"][pair_id]["observed_delta_w"]) * 1e9,
            "placement_typical_delta_nw": float(audits["typical"][pair_id]["observed_delta_w"]) * 1e9,
            "placement_peak_delta_nw": float(audits["peak"][pair_id]["observed_delta_w"]) * 1e9,
            "setup_margin_ns": min(float(row["setup_after_ns"]) - float(row["setup_before_ns"]) for row in audit_rows),
            "hold_margin_ns": min(float(row["hold_after_ns"]) - float(row["hold_before_ns"]) for row in audit_rows),
            "candidate_odb_sha256": digest(candidate_odb),
        })

    ranked = sorted(candidate_rows, key=lambda row: (float(row["predicted_worst_workload_delta_nw"]),
                                                      float(row["predicted_mean_workload_delta_nw"]),
                                                      str(row["pair_id"])))
    for rank, row in enumerate(ranked, start=1):
        row["model_rank"] = rank
        row["selection_role"] = "model_ranked" if rank <= args.top_count else "matched_control"
        row["model_sha256"] = digest(model_path)
        row["candidate_pool_sha256"] = digest(trial / "candidate_pool.tsv")
    write_table(trial / "candidate_scores.tsv", ranked)
    write_table(trial / "per_net_predictions.tsv", net_rows)
    selected = [row for row in ranked if row["selection_role"] == "model_ranked"]
    controls = [row for row in ranked if row["selection_role"] == "matched_control"]
    if not selected or not controls:
        raise ValueError("prospective trial needs at least one ranked candidate and one control")
    write_table(trial / "selection_manifest.tsv", selected + controls)
    print(f"Scored {len(ranked)} placement-legal, 50 ps timing-safe candidates using {model['model']}.")
    print(f"Frozen {len(selected)} model-ranked candidates and {len(controls)} lower-ranked controls before routing.")
    print(f"Scores: {trial / 'candidate_scores.tsv'}")
    print(f"Selection: {trial / 'selection_manifest.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
