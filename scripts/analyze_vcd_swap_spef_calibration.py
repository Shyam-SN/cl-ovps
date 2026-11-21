#!/usr/bin/env python3
"""Join routed pair-swap SPEF deltas to placement features and evaluate calibration."""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_TAG = os.environ.get("VCD_SWAP_AUDIT_TAG", "20261027_locsearch01")
WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
EPSILON = 1.0e-15
ACTIVITY_RE = re.compile(
    r"(?P<pin>\S+) transitions (?P<transitions>[-+0-9.eE]+) "
    r"activity (?P<density>[-+0-9.eE]+) duty (?P<duty>[-+0-9.eE]+)"
)
TOTAL_RE = re.compile(
    r"^Total\s+[-+0-9.eE]+\s+(?P<switching>[-+0-9.eE]+)", re.MULTILINE
)

OLD_CANDIDATES = {
    "place538:+460": ROOT / "build/power/place_spef_calibration_20261027_calset01a_place538_m460",
    "_12131_:-460": ROOT / "build/power/place_spef_calibration_20261027_002801__12131__m460",
    "place540:-460": ROOT / "build/power/place_spef_calibration_20261027_002801_place540_m460",
    "place544:+460": ROOT / "build/power/place_spef_calibration_20261027_002801_place544_p460",
}


def read_csv(path: Path, delimiter: str = ",") -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter=delimiter))


def read_integrity_manifest(path: Path) -> dict[str, str]:
    values = dict(
        line.split("\t", 1)
        for line in path.read_text().splitlines()
        if "\t" in line
    )
    for before, after in (
        ("baseline_place_odb_sha256", "baseline_place_odb_sha256_after"),
        ("candidate_place_odb_sha256", "candidate_place_odb_sha256_after"),
        ("placement_sdc_sha256", "placement_sdc_sha256_after"),
        ("routed_baseline_spef_sha256_before", "routed_baseline_spef_sha256_after"),
        ("routed_candidate_spef_sha256_before", "routed_candidate_spef_sha256_after"),
    ):
        if not values.get(before) or values[before] != values.get(after):
            raise ValueError(f"source-integrity mismatch for {before} in {path}")
    return values


def write_csv(path: Path, rows: list[dict[str, object]], delimiter: str = ",") -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as output:
        fields = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(output, fieldnames=fields, delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def read_place(directory: Path, workload: str) -> tuple[dict[str, dict[str, float | str]], float, int]:
    log = directory / f"{workload}.log"
    cap_file = directory / f"{workload}.driver_caps.tsv"
    text = log.read_text(errors="replace")
    activity = {
        match.group("pin"): float(match.group("density"))
        for line in text.splitlines()
        if (match := ACTIVITY_RE.search(line))
    }
    total = TOTAL_RE.search(text)
    if total is None:
        raise ValueError(f"missing OpenSTA switching total in {log}")
    nets: dict[str, dict[str, float | str]] = {}
    with cap_file.open(errors="replace") as source:
        for line in source:
            if not line.startswith("#DRIVER_CAP\t"):
                continue
            fields = line.rstrip("\n").split("\t")
            if len(fields) == 6 and fields[1] == "pin":
                continue
            if len(fields) != 6:
                raise ValueError(f"malformed placement capacitance row in {cap_file}: {line!r}")
            _, pin, net, pin_cap, wire_cap, load_cap = fields
            if net in nets:
                raise ValueError(f"multiple output drivers for {net} in {cap_file}")
            nets[net] = {
                "pin": pin,
                "pin_cap": float(pin_cap),
                "wire_cap": float(wire_cap),
                "load_cap": float(load_cap),
                "density": activity.get(pin, 0.0),
                "annotated": float(pin in activity),
            }
    return nets, float(total.group("switching")), len(activity)


def read_physical(directory: Path, candidate: str, stage: str) -> dict[str, dict[str, str]]:
    manifest = read_csv(directory / "physical_export_manifest.tsv", "\t")
    match = [row for row in manifest if row["candidate"] == candidate and row["stage"] == stage]
    if len(match) != 1:
        raise ValueError(f"expected one physical export for {candidate}/{stage}, found {len(match)}")
    return {row["net"]: row for row in read_csv(Path(match[0]["net_csv"]))}


def number(row: dict[str, str], key: str) -> float:
    return float(row.get(key, "0") or 0.0)


def enrich_rows(
    candidate: str,
    workload: str,
    baseline_place: dict[str, dict[str, float | str]],
    candidate_place: dict[str, dict[str, float | str]],
    routed_rows: list[dict[str, str]],
    physical: dict[str, dict[str, dict[str, str]]],
) -> list[dict[str, object]]:
    route = {row["net"]: row for row in routed_rows}
    pbase = physical[("baseline", "placement")]
    pcand = physical[(candidate, "placement")]
    rbase = physical[("baseline", "routed")]
    rcand = physical[(candidate, "routed")]
    factor = 0.5 * VOLTAGE**2
    records: list[dict[str, object]] = []
    common = [
        net for net, item in route.items()
        if item["baseline_activity_origin"] != "clock"
        and item["candidate_activity_origin"] != "clock"
        and not net.startswith("clknet_")
        and net in baseline_place
        and net in candidate_place
    ]
    if len(common) != 8584:
        raise ValueError(f"{candidate}/{workload}: expected 8,584 matched signal nets, found {len(common)}")

    for net in sorted(common):
        b = baseline_place[net]
        c = candidate_place[net]
        rr = route[net]
        base_density = float(b["density"])
        candidate_density = float(c["density"])
        delta_pin = float(c["pin_cap"]) - float(b["pin_cap"])
        delta_wire = float(c["wire_cap"]) - float(b["wire_cap"])
        delta_load = float(c["load_cap"]) - float(b["load_cap"])
        placed_prediction = factor * (
            float(c["load_cap"]) * candidate_density - float(b["load_cap"]) * base_density
        )
        placement_cap = factor * base_density * delta_load
        placement_activity = factor * float(c["load_cap"]) * (candidate_density - base_density)
        p0 = pbase.get(net)
        p1 = pcand.get(net)
        r0 = rbase.get(net)
        r1 = rcand.get(net)
        if any(item is None for item in (p0, p1, r0, r1)):
            raise ValueError(f"missing physical features for {candidate}/{net}")
        assert p0 is not None and p1 is not None and r0 is not None and r1 is not None
        dbu = number(p1, "dbu_per_micron")
        place_hpwl_delta = (number(p1, "hpwl_dbu") - number(p0, "hpwl_dbu")) / dbu
        route_centerline_delta = (number(r1, "routed_centerline_dbu") - number(r0, "routed_centerline_dbu")) / dbu
        route_via_delta = number(r1, "via_count") - number(r0, "via_count")
        routed_delta_power = number(rr, "delta_switching_power_w")
        routed_delta_wire = number(rr, "delta_wire_cap_f")
        records.append({
            "candidate": candidate,
            "workload": workload,
            "net": net,
            "routed_class": "common_signal",
            "routed_activity_origin": rr["baseline_activity_origin"],
            "baseline_place_density_hz": base_density,
            "candidate_place_density_hz": candidate_density,
            "delta_place_pin_cap_f": delta_pin,
            "delta_place_wire_cap_f": delta_wire,
            "delta_place_load_cap_f": delta_load,
            "place_capacitance_power_delta_w": placement_cap,
            "place_activity_power_delta_w": placement_activity,
            "place_predicted_power_delta_w": placed_prediction,
            "routed_delta_wire_cap_f": routed_delta_wire,
            "routed_delta_load_cap_f": number(rr, "delta_load_cap_f"),
            "routed_activity_power_delta_w": number(rr, "activity_contribution_w"),
            "routed_power_delta_w": routed_delta_power,
            "place_minus_routed_error_w": placed_prediction - routed_delta_power,
            "place_delta_hpwl_um": place_hpwl_delta,
            "place_abs_delta_hpwl_um": abs(place_hpwl_delta),
            "route_delta_centerline_um": route_centerline_delta,
            "route_abs_delta_centerline_um": abs(route_centerline_delta),
            "route_delta_vias": route_via_delta,
            "place_baseline_fanout": number(p0, "fanout"),
            "place_candidate_fanout": number(p1, "fanout"),
        })
    return records


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    xm = sum(xs) / len(xs)
    ym = sum(ys) / len(ys)
    xx = sum((x - xm) ** 2 for x in xs)
    yy = sum((y - ym) ** 2 for y in ys)
    if xx <= 0 or yy <= 0:
        return None
    return sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(order):
        end = i + 1
        while end < len(order) and values[order[end]] == values[order[i]]:
            end += 1
        rank = (i + 1 + end) / 2.0
        for j in range(i, end):
            result[order[j]] = rank
        i = end
    return result


def mae(actual: list[float], predicted: list[float]) -> float:
    return sum(abs(y - p) for y, p in zip(actual, predicted)) / max(1, len(actual))


def fit_scale(xs: list[float], ys: list[float]) -> float:
    denom = sum(x * x for x in xs)
    return sum(x * y for x, y in zip(xs, ys)) / denom if denom else 0.0


def evaluate_lomo(records: list[dict[str, object]], features: dict[str, callable]) -> list[dict[str, object]]:
    labels = sorted({str(row["candidate"]) for row in records})
    actual = [float(row["routed_power_delta_w"]) for row in records]
    result: list[dict[str, object]] = []
    for name, get_x in features.items():
        xs = [float(get_x(row)) for row in records]
        held_predictions = [0.0] * len(records)
        scales: dict[str, float] = {}
        for held in labels:
            train_i = [i for i, row in enumerate(records) if str(row["candidate"]) != held]
            test_i = [i for i, row in enumerate(records) if str(row["candidate"]) == held]
            scale = fit_scale([xs[i] for i in train_i], [actual[i] for i in train_i])
            scales[held] = scale
            for i in test_i:
                held_predictions[i] = scale * xs[i]
        result.append({
            "feature": name,
            "scope": "per-net common-signal, leave-one-candidate-out",
            "rows": len(records),
            "zero_mae_w": mae(actual, [0.0] * len(actual)),
            "raw_mae_w": mae(actual, xs)
            if name in {"placement_power_prediction", "placement_wire_cap_power_proxy"}
            else "",
            "loco_scaled_mae_w": mae(actual, held_predictions),
            "pearson_r": correlation(xs, actual) if correlation(xs, actual) is not None else "",
            "spearman_rho": correlation(ranks(xs), ranks(actual)) if correlation(xs, actual) is not None else "",
            "heldout_scales": ";".join(f"{label}={scales[label]:.6g}" for label in labels),
        })
    return result


def evaluate_candidate_lomo(
    summaries: list[dict[str, object]], feature: str, target: str
) -> tuple[float, float, float]:
    labels = sorted({str(row["candidate"]) for row in summaries})
    xs = [float(row[feature]) for row in summaries]
    ys = [float(row[target]) for row in summaries]
    pred = [0.0] * len(summaries)
    for held in labels:
        train = [i for i, row in enumerate(summaries) if str(row["candidate"]) != held]
        test = [i for i, row in enumerate(summaries) if str(row["candidate"]) == held]
        scale = fit_scale([xs[i] for i in train], [ys[i] for i in train])
        for i in test:
            pred[i] = scale * xs[i]
    return mae(ys, [0.0] * len(ys)), mae(ys, xs), mae(ys, pred)


def old_aggregate_rows(records: list[dict[str, object]]) -> list[dict[str, object]]:
    by_group: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in records:
        by_group[(str(row["candidate"]), str(row["workload"]))].append(row)
    result = []
    for (candidate, workload), rows in sorted(by_group.items()):
        source_dir = OLD_CANDIDATES[candidate]
        all_rows = read_csv(source_dir / f"{workload}.placement_vs_routed.per_net.csv")
        clock = sum(number(row, "routed_power_delta_w") for row in all_rows if row["routed_class"] == "postcts_clock")
        other = sum(number(row, "routed_power_delta_w") for row in all_rows
                    if row["routed_class"] == "not_common_to_placement" and row["routed_activity_origin"] != "clock")
        wire = sum(number(row, "routed_delta_wire_cap_f") for row in all_rows)
        result.append({
            "candidate": candidate,
            "workload": workload,
            "placement_signal_prediction_w": sum(float(row["place_predicted_power_delta_w"]) for row in rows),
            "routed_signal_delta_w": sum(float(row["routed_power_delta_w"]) for row in rows),
            "routed_clock_delta_w": clock,
            "routed_other_nonclock_delta_w": other,
            "routed_total_per_net_delta_w": sum(number(row, "routed_power_delta_w") for row in all_rows),
            "routed_total_wire_cap_delta_f": wire,
        })
    return result


def new_aggregate_rows(
    records: list[dict[str, object]], pair_dir: Path
) -> list[dict[str, object]]:
    by_group: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in records:
        by_group[(str(row["candidate"]), str(row["workload"]))].append(row)
    result = []
    for candidate in ("swap44", "swap52"):
        report_dir = ROOT / f"build/power/vcdswap_{AUDIT_TAG}_{candidate}"
        route_dir = report_dir / "net_attribution"
        for workload in WORKLOADS:
            rows = by_group[(candidate, workload)]
            all_rows = read_csv(route_dir / f"{workload}.per_net_delta.csv")
            clock = sum(number(row, "delta_switching_power_w") for row in all_rows
                        if row["baseline_activity_origin"] == "clock" or row["net"].startswith("clknet_"))
            common_nets = {str(row["net"]) for row in rows}
            other = sum(number(row, "delta_switching_power_w") for row in all_rows
                        if row["net"] not in common_nets
                        and row["baseline_activity_origin"] != "clock"
                        and not row["net"].startswith("clknet_"))
            result.append({
                "candidate": candidate,
                "workload": workload,
                "placement_signal_prediction_w": sum(float(row["place_predicted_power_delta_w"]) for row in rows),
                "routed_signal_delta_w": sum(float(row["routed_power_delta_w"]) for row in rows),
                "routed_clock_delta_w": clock,
                "routed_other_nonclock_delta_w": other,
                "routed_total_per_net_delta_w": sum(number(row, "delta_switching_power_w") for row in all_rows),
                "routed_total_wire_cap_delta_f": sum(number(row, "delta_wire_cap_f") for row in all_rows),
                "placement_reported_switching_delta_w": "",
            })
            result[-1]["placement_reported_switching_delta_w"] = (
                _reported_total(pair_dir / "placement" / candidate / f"{workload}.log")
                - _reported_total(pair_dir / "placement/baseline" / f"{workload}.log")
            )
    return result


def _reported_total(path: Path) -> float:
    match = TOTAL_RE.search(path.read_text(errors="replace"))
    if match is None:
        raise ValueError(f"missing OpenSTA switching total in {path}")
    return float(match.group("switching"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--old-analysis-dir", type=Path,
        default=ROOT / "build/power/place_spef_feature_analysis_20261027_002801",
    )
    parser.add_argument(
        "--pair-dir", type=Path,
        default=ROOT / f"build/power/vcd_swap_audit_{AUDIT_TAG}/pair_calibration",
    )
    args = parser.parse_args()
    out = args.output_dir.resolve()
    pair_dir = args.pair_dir.resolve()
    try:
        old_rows = read_csv(args.old_analysis_dir / "pooled_per_net_features.csv")
        if len(old_rows) != 4 * len(WORKLOADS) * 8584:
            raise ValueError(f"unexpected previous calibration row count: {len(old_rows)}")
        for label, directory in OLD_CANDIDATES.items():
            if not (directory / "summary.txt").is_file():
                raise FileNotFoundError(f"missing previous calibration for {label}: {directory}")
        old_manifests = {
            label: read_integrity_manifest(directory / "manifest.tsv")
            for label, directory in OLD_CANDIDATES.items()
        }
        shared_keys = (
            "baseline_place_odb_sha256",
            "placement_sdc_sha256",
            "routed_baseline_spef_sha256_before",
        )
        for key in shared_keys:
            values = {manifest.get(key) for manifest in old_manifests.values()}
            if len(values) != 1 or not next(iter(values)):
                raise ValueError(f"existing calibration samples do not share {key}")

        physical_dir = pair_dir / "physical_features"
        physical = {
            (candidate, stage): read_physical(physical_dir, candidate, stage)
            for candidate in ("baseline", "swap44", "swap52")
            for stage in ("placement", "routed")
        }
        placement_root = pair_dir / "placement"
        pair_records: list[dict[str, object]] = []
        for candidate in ("swap44", "swap52"):
            report_dir = ROOT / f"build/power/vcdswap_{AUDIT_TAG}_{candidate}/net_attribution"
            for workload in WORKLOADS:
                base_place, _, base_annotated = read_place(placement_root / "baseline", workload)
                cand_place, _, cand_annotated = read_place(placement_root / candidate, workload)
                if base_annotated != 32140 or cand_annotated != 32140:
                    raise ValueError(f"unexpected VCD annotation coverage for {candidate}/{workload}")
                routed = read_csv(report_dir / f"{workload}.per_net_delta.csv")
                pair_records.extend(enrich_rows(candidate, workload, base_place, cand_place, routed, physical))

        # Normalize the old calibration rows to the same columns, retaining their previously checked physical joins.
        records: list[dict[str, object]] = []
        for row in old_rows:
            normalized = dict(row)
            normalized["place_delta_hpwl_um"] = row.get("place_delta_hpwl_um", "0")
            normalized["place_abs_delta_hpwl_um"] = row.get("place_abs_delta_hpwl_um", "0")
            normalized["route_delta_centerline_um"] = row.get("route_delta_centerline_um", "0")
            normalized["route_abs_delta_centerline_um"] = row.get("route_abs_delta_centerline_um", "0")
            normalized["route_delta_vias"] = row.get("route_delta_vias", "0")
            records.append(normalized)
        old_coverage = defaultdict(int)
        for row in old_rows:
            old_coverage[(row["candidate"], row["workload"])] += 1
        if any(count != 8584 for count in old_coverage.values()) or len(old_coverage) != 4 * len(WORKLOADS):
            raise ValueError("previous calibration pool does not contain 8,584 rows per candidate/workload")
        records.extend(pair_records)
        if len(records) != 6 * len(WORKLOADS) * 8584:
            raise ValueError(f"expected 154,512 joined rows, found {len(records)}")

        aggregate = old_aggregate_rows(records[:len(old_rows)]) + new_aggregate_rows(pair_records, pair_dir)
        metric_features = {
            "placement_power_prediction": lambda r: float(r["place_predicted_power_delta_w"]),
            "placement_wire_cap_power_proxy": lambda r: 0.5 * VOLTAGE**2 * float(r["baseline_place_density_hz"]) * float(r["delta_place_wire_cap_f"]),
            "placement_hpwl_delta_um": lambda r: float(r["place_delta_hpwl_um"]),
            "routed_centerline_delta_um (post-route diagnostic)": lambda r: float(r["route_delta_centerline_um"]),
            "routed_via_delta (post-route diagnostic)": lambda r: float(r["route_delta_vias"]),
            "routed_wire_cap_power_proxy (post-route upper bound)": lambda r: 0.5 * VOLTAGE**2 * float(r["baseline_place_density_hz"]) * float(r["routed_delta_wire_cap_f"]),
        }
        lomo = evaluate_lomo(records, metric_features)
        write_csv(out / "matched_per_net_features.csv", records)
        write_csv(out / "candidate_workload_attribution.tsv", aggregate, "\t")
        write_csv(out / "loco_model_metrics.tsv", lomo, "\t")

        candidate_metrics: list[dict[str, object]] = []
        for feature, target in (
            ("placement_signal_prediction_w", "routed_signal_delta_w"),
            ("placement_signal_prediction_w", "routed_total_per_net_delta_w"),
        ):
            zero, raw, lomo_mae = evaluate_candidate_lomo(aggregate, feature, target)
            candidate_metrics.append({
                "feature": feature,
                "target": target,
                "rows": len(aggregate),
                "zero_mae_w": zero,
                "raw_mae_w": raw,
                "loco_scaled_mae_w": lomo_mae,
            })
        write_csv(out / "candidate_loco_metrics.tsv", candidate_metrics, "\t")

        zero_place_rows = [
            row for row in records
            if abs(float(row["place_predicted_power_delta_w"])) <= EPSILON
            and abs(float(row["routed_power_delta_w"])) > EPSILON
        ]
        routed_change_rows = [row for row in records if abs(float(row["routed_power_delta_w"])) > EPSILON]
        signal_direction_rows = [
            row for row in aggregate
            if abs(float(row["placement_signal_prediction_w"])) > EPSILON
            and abs(float(row["routed_signal_delta_w"])) > EPSILON
        ]
        signal_direction_matches = sum(
            float(row["placement_signal_prediction_w"]) * float(row["routed_signal_delta_w"]) > 0.0
            for row in signal_direction_rows
        )
        total_direction_rows = [
            row for row in aggregate
            if abs(float(row["placement_signal_prediction_w"])) > EPSILON
            and abs(float(row["routed_total_per_net_delta_w"])) > EPSILON
        ]
        total_direction_matches = sum(
            float(row["placement_signal_prediction_w"]) * float(row["routed_total_per_net_delta_w"]) > 0.0
            for row in total_direction_rows
        )

        lines = [
            "# Placement-to-SPEF calibration with the two VCD-guided pair swaps",
            "",
            "## Coverage and method",
            "",
            "Joined the existing four one-cell calibration candidates with swap44 and swap52. For each candidate and idle/typical/peak workload, the joined table contains all 8,584 common signal nets: placement-side VCD activity and estimated capacitance, routed SPEF wire/load capacitance and switching-power delta, placement HPWL, and final-route centerline/via changes. The two pair-swap candidate placement reports each annotated 32,140 pins; ODB geometry exports hash-checked their source checkpoints before and after. The prior four calibration reports already share the same baseline placement ODB, SDC, and routed baseline SPEF.",
            "",
            "Clock-tree nets are excluded from the placed-net model fit because they do not exist in the pre-CTS placement ODB. Their routed deltas are retained in the attribution table and reported separately from common signal nets. The zero model predicts no per-net change. Leave-one-candidate-out (LOCO) fits one through-origin scale on five candidate layouts and scores all three workloads of the held-out candidate; no net from a held-out candidate enters its fit.",
            "",
            "## Whole-layout routed attribution",
            "",
            "Power values below are switching-power deltas in µW; negative means lower than baseline. Placement prediction is pre-CTS common-signal only. Routed total is the per-net SPEF sum, with clock and route-only non-clock deltas shown separately.",
            "",
            "| Candidate | Workload | Placement signal estimate | Routed signal | Routed clock | Other non-clock | Routed total |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for row in aggregate:
            lines.append(
                f"| {row['candidate']} | {row['workload']} | {float(row['placement_signal_prediction_w'])*1e6:+.3f} | "
                f"{float(row['routed_signal_delta_w'])*1e6:+.3f} | {float(row['routed_clock_delta_w'])*1e6:+.3f} | "
                f"{float(row['routed_other_nonclock_delta_w'])*1e6:+.3f} | {float(row['routed_total_per_net_delta_w'])*1e6:+.3f} |"
            )
        lines.extend([
            "",
            "## Held-out model comparison",
            "",
            "Per-net common-signal MAE is reported in nW/net. The candidate-level comparison scores 18 candidate/workload cases (six layouts × three workloads); its target is first the routed common-signal subtotal and then the full routed SPEF delta, including clock changes.",
            "",
            "### Per-net LOCO",
            "",
            "| Feature | Zero MAE | Raw MAE | LOCO scaled MAE | Pearson r | Spearman ρ |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for row in lomo:
            raw = "—" if row["raw_mae_w"] == "" else f"{float(row['raw_mae_w'])*1e9:.4f}"
            pearson = "—" if row["pearson_r"] == "" else f"{float(row['pearson_r']):+.4f}"
            spearman = "—" if row["spearman_rho"] == "" else f"{float(row['spearman_rho']):+.4f}"
            lines.append(
                f"| {row['feature']} | {float(row['zero_mae_w'])*1e9:.4f} | {raw} | "
                f"{float(row['loco_scaled_mae_w'])*1e9:.4f} | {pearson} | {spearman} |"
            )
        lines.extend([
            "",
            "## Direction and sparsity checks",
            "",
            f"Only {sum(abs(float(row['place_predicted_power_delta_w'])) > EPSILON for row in records)} of {len(records)} joined net/workload rows have a placement prediction above 1e-15 W. Of {len(routed_change_rows)} rows with a routed SPEF power change above that threshold, {len(zero_place_rows)} ({len(zero_place_rows)/max(1, len(routed_change_rows)):.2%}) have essentially zero placement prediction.",
            f"At candidate/workload level, raw placement-score direction matched the routed signal subtotal in {signal_direction_matches}/{len(signal_direction_rows)} cases with nonzero predictions, and matched the full routed delta (including clock) in {total_direction_matches}/{len(total_direction_rows)} such cases.",
            "",
            "### Candidate-level LOCO",
            "",
            "| Target | Zero MAE | Raw placement score MAE | LOCO-scaled MAE |",
            "|---|---:|---:|---:|",
        ])
        for row in candidate_metrics:
            target_name = "Routed signal subtotal" if row["target"] == "routed_signal_delta_w" else "Full routed total (clock included)"
            lines.append(
                f"| {target_name} | {float(row['zero_mae_w'])*1e6:.3f} µW | "
                f"{float(row['raw_mae_w'])*1e6:.3f} µW | {float(row['loco_scaled_mae_w'])*1e6:.3f} µW |"
            )
        lines.extend([
            "",
            "## Decision",
            "",
            "Use the placement activity-weighted capacitance score as a candidate generator only; it is not a calibrated final power predictor yet. A calibration is useful only if its held-out MAE beats the zero-change baseline and correctly predicts routed direction. The clock-tree column is a material additional term that the current pre-CTS score cannot see. Route centerline/via features are post-route diagnostics, not legal pre-route inputs; the routed-SPEF wire-cap proxy is an upper-bound reconciliation check, not a placer feature.",
            "",
            "The six candidates are small perturbations of one PicoRV32 placement, not independent designs. This result is exploratory and should not be treated as a general model. The next useful data is a larger, varied set of legal moves with matched placement and routed SPEF, followed by held-out evaluation on entire moves or a separate design.",
            "",
            "## Generated data",
            "",
            "- `matched_per_net_features.csv`: 154,512 placement-to-SPEF matched signal-net rows.",
            "- `candidate_workload_attribution.tsv`: signal, clock, other non-clock, and total deltas for each candidate/workload.",
            "- `loco_model_metrics.tsv`: per-net zero/raw/LOCO metrics and scales.",
            "- `candidate_loco_metrics.tsv`: held-out candidate/workload aggregate errors.",
        ])
        (out / "calibration_report.md").write_text("\n".join(lines) + "\n")
        print(out / "calibration_report.md")
        print(f"Matched per-net records: {len(records)}")
        return 0
    except (OSError, KeyError, ValueError, StopIteration) as exc:
        print(f"pair-swap calibration analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
