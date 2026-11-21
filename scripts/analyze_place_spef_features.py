#!/usr/bin/env python3
"""Pool placement/SPEF per-net data and run coefficient-free feature diagnostics."""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
EPSILON = 1.0e-15
PLACE_RE = re.compile(
    r"Placement OpenSTA delta: (?P<reported>[+-]?[0-9.eE+-]+) W; "
    r"placement per-net delta: (?P<pernet>[+-]?[0-9.eE+-]+) W;"
)
ROUTE_RE = re.compile(
    r"Routed OpenSTA delta: (?P<reported>[+-]?[0-9.eE+-]+) W; "
    r"routed per-net delta: (?P<pernet>[+-]?[0-9.eE+-]+) W\."
)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    xmean = sum(xs) / len(xs)
    ymean = sum(ys) / len(ys)
    xx = sum((x - xmean) ** 2 for x in xs)
    yy = sum((y - ymean) ** 2 for y in ys)
    if xx <= 0.0 or yy <= 0.0:
        return None
    return sum((x - xmean) * (y - ymean) for x, y in zip(xs, ys)) / math.sqrt(xx * yy)


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        value = values[order[index]]
        while end < len(order) and values[order[end]] == value:
            end += 1
        rank = (index + 1 + end) / 2.0
        for sorted_index in range(index, end):
            result[order[sorted_index]] = rank
        index = end
    return result


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(ranks(xs), ranks(ys))


def mae(actual: list[float], predicted: list[float]) -> float:
    return sum(abs(y - p) for y, p in zip(actual, predicted)) / max(1, len(actual))


def rmse(actual: list[float], predicted: list[float]) -> float:
    return math.sqrt(sum((y - p) ** 2 for y, p in zip(actual, predicted)) / max(1, len(actual)))


def fit_slope_through_origin(xs: list[float], ys: list[float]) -> float:
    denominator = sum(x * x for x in xs)
    return sum(x * y for x, y in zip(xs, ys)) / denominator if denominator > 0 else 0.0


def parse_calibration_summary(path: Path) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    current = ""
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if stripped in {"idle:", "typical:", "peak:"}:
            current = stripped[:-1]
            result[current] = {}
            continue
        if not current:
            continue
        place = PLACE_RE.search(stripped)
        route = ROUTE_RE.search(stripped)
        if place:
            result[current]["place_reported"] = float(place.group("reported"))
            result[current]["place_pernet"] = float(place.group("pernet"))
        if route:
            result[current]["route_reported"] = float(route.group("reported"))
            result[current]["route_pernet"] = float(route.group("pernet"))
            current = ""
    if set(result) != set(WORKLOADS) or any(len(values) != 4 for values in result.values()):
        raise ValueError(f"could not parse placement/routed reconciliation metrics from {path}")
    return result


def verify_manifest(path: Path) -> dict[str, str]:
    values = dict(
        line.split("\t", 1) for line in path.read_text().splitlines() if "\t" in line
    )
    pairs = (
        ("baseline_place_odb_sha256", "baseline_place_odb_sha256_after"),
        ("candidate_place_odb_sha256", "candidate_place_odb_sha256_after"),
        ("placement_sdc_sha256", "placement_sdc_sha256_after"),
        ("routed_baseline_spef_sha256_before", "routed_baseline_spef_sha256_after"),
        ("routed_candidate_spef_sha256_before", "routed_candidate_spef_sha256_after"),
    )
    for before, after in pairs:
        if not values.get(before) or values[before] != values.get(after):
            raise ValueError(f"calibration input integrity check failed for {before} in {path}")
    return values


def read_candidate(label: str, calibration_dir: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, float]]]:
    summary = parse_calibration_summary(calibration_dir / "summary.txt")
    records: list[dict[str, str]] = []
    for workload in WORKLOADS:
        path = calibration_dir / f"{workload}.placement_vs_routed.per_net.csv"
        with path.open(newline="") as source:
            rows = list(csv.DictReader(source))
        seen: set[str] = set()
        for row in rows:
            if row["net"] in seen:
                raise ValueError(f"duplicate net {row['net']} in {path}")
            seen.add(row["net"])
            row["candidate"] = label
            row["workload"] = workload
            records.append(row)
        common = [row for row in rows if row["routed_class"] == "common_signal"]
        if len(common) != 8584:
            raise ValueError(f"expected 8584 matched common signal nets in {path}; found {len(common)}")
        for key, column in (("place_pernet", "place_predicted_power_delta_w"),
                             ("route_pernet", "routed_power_delta_w")):
            actual = sum(float(row[column]) for row in rows)
            expected = summary[workload][key]
            if abs(actual - expected) > 5.0e-9:
                raise ValueError(
                    f"{label}/{workload} {key} does not reconcile: CSV {actual:.6e} W vs summary {expected:.6e} W"
                )
    return records, summary


def numeric(row: dict[str, str], key: str) -> float:
    return float(row.get(key, "0") or 0.0)


BASE_DATA_COLUMNS = [
    "candidate", "workload", "net", "routed_class", "routed_activity_origin",
    "baseline_place_density_hz", "candidate_place_density_hz",
    "delta_place_pin_cap_f", "delta_place_wire_cap_f", "delta_place_load_cap_f",
    "place_capacitance_power_delta_w", "place_activity_power_delta_w",
    "place_predicted_power_delta_w", "routed_delta_wire_cap_f", "routed_delta_load_cap_f",
    "routed_activity_power_delta_w", "routed_power_delta_w", "place_minus_routed_error_w",
]


def read_net_features(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["net"] in result:
            raise ValueError(f"duplicate physical net {row['net']} in {path}")
        result[row["net"]] = row
    return result


def read_layer_features(path: Path) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    with path.open(newline="") as source:
        for row in csv.DictReader(source):
            per_net = result.setdefault(row["net"], {})
            if row["layer"] in per_net:
                raise ValueError(f"duplicate route layer {row['layer']} for {row['net']} in {path}")
            per_net[row["layer"]] = float(row["centerline_dbu"])
    return result


def merge_physical_features(
    records: list[dict[str, str]],
    physical_dir: Path,
    labels: list[str],
    prior_integrity: dict[str, str],
    candidate_integrities: dict[str, dict[str, str]],
) -> list[str]:
    manifest_path = physical_dir / "physical_export_manifest.tsv"
    with manifest_path.open(newline="") as source:
        manifest_rows = list(csv.DictReader(source, delimiter="\t"))
    exports: dict[tuple[str, str], dict[str, str]] = {}
    for export in manifest_rows:
        key = (export["candidate"], export["stage"])
        if key in exports:
            raise ValueError(f"duplicate physical export manifest entry: {key}")
        if sha256_file(Path(export["odb"])) != export["odb_sha256"]:
            raise ValueError(f"physical feature source ODB hash changed since export: {export['odb']}")
        exports[key] = export

    expected = {("baseline", "placement"), ("baseline", "routed")}
    expected.update((label, stage) for label in labels for stage in ("placement", "routed"))
    if set(exports) != expected:
        raise ValueError(f"physical export coverage mismatch; expected {sorted(expected)}, found {sorted(exports)}")
    if exports[("baseline", "placement")]["odb_sha256"] != prior_integrity.get("baseline_place_odb_sha256"):
        raise ValueError("physical baseline placement ODB does not match calibration manifest")
    for label in labels:
        expected_hash = candidate_integrities[label]["candidate_place_odb_sha256"]
        if exports[(label, "placement")]["odb_sha256"] != expected_hash:
            raise ValueError(f"physical candidate placement ODB does not match calibration for {label}")

    net_data: dict[tuple[str, str], dict[str, dict[str, str]]] = {}
    layer_data: dict[tuple[str, str], dict[str, dict[str, float]]] = {}
    for key, export in exports.items():
        net_data[key] = read_net_features(Path(export["net_csv"]))
        layer_data[key] = read_layer_features(Path(export["layer_csv"]))

    layers = sorted({
        layer
        for values in layer_data.values()
        for per_net in values.values()
        for layer in per_net
    })
    physical_columns = [
        "place_baseline_fanout", "place_candidate_fanout", "place_delta_fanout",
        "place_baseline_endpoint_count", "place_candidate_endpoint_count", "place_delta_endpoint_count",
        "place_baseline_hpwl_um", "place_candidate_hpwl_um", "place_delta_hpwl_um",
        "place_abs_delta_hpwl_um", "place_delta_bbox_width_um", "place_delta_bbox_height_um",
        "route_baseline_centerline_um", "route_candidate_centerline_um", "route_delta_centerline_um",
        "route_abs_delta_centerline_um", "route_baseline_vias", "route_candidate_vias",
        "route_delta_vias", "route_has_baseline_wire", "route_has_candidate_wire",
    ] + [f"route_delta_{layer}_um" for layer in layers]

    def number(row: dict[str, str], key: str) -> float:
        return float(row.get(key, "0") or 0.0)

    for row in records:
        label = row["candidate"]
        net = row["net"]
        p0 = net_data[("baseline", "placement")].get(net)
        p1 = net_data[(label, "placement")].get(net)
        r0 = net_data[("baseline", "routed")].get(net)
        r1 = net_data[(label, "routed")].get(net)
        if any(item is None for item in (p0, p1, r0, r1)):
            missing = [name for name, item in (("baseline placement", p0), ("candidate placement", p1),
                                                ("baseline routed", r0), ("candidate routed", r1)) if item is None]
            raise ValueError(f"physical features missing for {label}/{net}: {', '.join(missing)}")
        assert p0 is not None and p1 is not None and r0 is not None and r1 is not None
        dbu = number(p1, "dbu_per_micron")
        base_hpwl = number(p0, "hpwl_dbu") / dbu
        cand_hpwl = number(p1, "hpwl_dbu") / dbu
        base_route = number(r0, "routed_centerline_dbu") / dbu
        cand_route = number(r1, "routed_centerline_dbu") / dbu
        base_layers = layer_data[("baseline", "routed")].get(net, {})
        cand_layers = layer_data[(label, "routed")].get(net, {})
        values: dict[str, float | int | str] = {
            "place_baseline_fanout": int(number(p0, "fanout")),
            "place_candidate_fanout": int(number(p1, "fanout")),
            "place_delta_fanout": int(number(p1, "fanout") - number(p0, "fanout")),
            "place_baseline_endpoint_count": int(number(p0, "endpoint_count")),
            "place_candidate_endpoint_count": int(number(p1, "endpoint_count")),
            "place_delta_endpoint_count": int(number(p1, "endpoint_count") - number(p0, "endpoint_count")),
            "place_baseline_hpwl_um": base_hpwl,
            "place_candidate_hpwl_um": cand_hpwl,
            "place_delta_hpwl_um": cand_hpwl - base_hpwl,
            "place_abs_delta_hpwl_um": abs(cand_hpwl - base_hpwl),
            "place_delta_bbox_width_um": (number(p1, "bbox_width_dbu") - number(p0, "bbox_width_dbu")) / dbu,
            "place_delta_bbox_height_um": (number(p1, "bbox_height_dbu") - number(p0, "bbox_height_dbu")) / dbu,
            "route_baseline_centerline_um": base_route,
            "route_candidate_centerline_um": cand_route,
            "route_delta_centerline_um": cand_route - base_route,
            "route_abs_delta_centerline_um": abs(cand_route - base_route),
            "route_baseline_vias": int(number(r0, "via_count")),
            "route_candidate_vias": int(number(r1, "via_count")),
            "route_delta_vias": int(number(r1, "via_count") - number(r0, "via_count")),
            "route_has_baseline_wire": r0["has_route"],
            "route_has_candidate_wire": r1["has_route"],
        }
        for layer in layers:
            values[f"route_delta_{layer}_um"] = (cand_layers.get(layer, 0.0) - base_layers.get(layer, 0.0)) / dbu
        row.update({key: str(value) for key, value in values.items()})
    return physical_columns


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def output_dataset(path: Path, records: list[dict[str, str]], extra_columns: list[str] | None = None) -> None:
    columns = BASE_DATA_COLUMNS + (extra_columns or [])
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in records:
            writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", type=Path, required=True)
    parser.add_argument("--prior-calibration", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--physical-dir", type=Path)
    args = parser.parse_args()
    try:
        with (args.batch_dir / "batch_manifest.tsv").open(newline="") as source:
            candidates = list(csv.DictReader(source, delimiter="\t"))
        if not candidates:
            raise ValueError("batch manifest contains no candidate moves")

        prior_integrity = verify_manifest(args.prior_calibration / "manifest.tsv")
        records, prior_summary = read_candidate("place538:+460", args.prior_calibration)
        candidate_summaries = {"place538:+460": prior_summary}
        labels = ["place538:+460"]
        candidate_integrities = {"place538:+460": prior_integrity}
        shared_baseline_keys = (
            "baseline_place_odb_sha256",
            "placement_sdc_sha256",
            "routed_baseline_spef_sha256_before",
        )
        for key in shared_baseline_keys:
            if not prior_integrity.get(key):
                raise ValueError(f"missing shared baseline integrity value {key}")
        for candidate in candidates:
            dx = int(candidate["dx_dbu"])
            label = f"{candidate['instance']}:{dx:+d}"
            if label in labels:
                raise ValueError(f"duplicate candidate label: {label}")
            calibration_dir = Path(candidate["calibration_report"])
            integrity = verify_manifest(calibration_dir / "manifest.tsv")
            for key in shared_baseline_keys:
                if integrity.get(key) != prior_integrity.get(key):
                    raise ValueError(f"calibrations do not share the same {key}")
            new_records, move_summary = read_candidate(label, calibration_dir)
            records.extend(new_records)
            candidate_summaries[label] = move_summary
            candidate_integrities[label] = integrity
            labels.append(label)

        common = [row for row in records if row["routed_class"] == "common_signal"]
        clock = [row for row in records if row["routed_class"] == "postcts_clock"]
        if len(common) != len(labels) * len(WORKLOADS) * 8584:
            raise ValueError("pooled common-signal row count does not match candidate/workload coverage")
        physical_columns: list[str] = []
        if args.physical_dir is not None:
            physical_columns = merge_physical_features(
                common, args.physical_dir, labels, prior_integrity, candidate_integrities
            )

        feature_functions = {
            "placement_power_prediction": lambda row: numeric(row, "place_predicted_power_delta_w"),
            "placement_wire_cap_power_proxy": lambda row: (
                0.5 * VOLTAGE**2 * numeric(row, "baseline_place_density_hz") * numeric(row, "delta_place_wire_cap_f")
            ),
            "placement_load_cap_power_proxy": lambda row: (
                0.5 * VOLTAGE**2 * numeric(row, "baseline_place_density_hz") * numeric(row, "delta_place_load_cap_f")
            ),
            "placement_wire_cap_delta": lambda row: numeric(row, "delta_place_wire_cap_f"),
            "placement_load_cap_delta": lambda row: numeric(row, "delta_place_load_cap_f"),
        }
        if physical_columns:
            feature_functions.update({
                "placement_hpwl_delta_um": lambda row: numeric(row, "place_delta_hpwl_um"),
                "placement_fanout_delta": lambda row: numeric(row, "place_delta_fanout"),
                "routed_centerline_delta_um": lambda row: numeric(row, "route_delta_centerline_um"),
                "routed_via_delta": lambda row: numeric(row, "route_delta_vias"),
            })
        actual_power = [numeric(row, "routed_power_delta_w") for row in common]
        actual_wire = [numeric(row, "routed_delta_wire_cap_f") for row in common]
        metric_rows: list[dict[str, str | int | float]] = []
        lomo_lines: list[str] = []

        def add_metric(feature: str, target: str, xs: list[float], ys: list[float], scope: str) -> None:
            r = pearson(xs, ys)
            rho = spearman(xs, ys)
            metric_rows.append({
                "feature": feature,
                "target": target,
                "scope": scope,
                "rows": len(xs),
                "pearson_r": r if r is not None else "",
                "spearman_rho": rho if rho is not None else "",
            })

        for feature, get_x in feature_functions.items():
            xs = [get_x(row) for row in common]
            add_metric(feature, "routed_power_delta_w", xs, actual_power, "all common-signal net/workload rows")
            if feature in {
                "placement_power_prediction", "placement_wire_cap_power_proxy", "placement_load_cap_power_proxy",
                "placement_hpwl_delta_um", "routed_centerline_delta_um", "routed_via_delta",
            }:
                placed = [row for row in common]
                y = [numeric(row, "routed_power_delta_w") for row in placed]
                x = [get_x(row) for row in placed]
                zero_mae = mae(y, [0.0] * len(y))
                held_predictions: list[float] = []
                slopes: dict[str, float] = {}
                for held_out in labels:
                    train_rows = [row for row in placed if row["candidate"] != held_out]
                    test_rows = [row for row in placed if row["candidate"] == held_out]
                    slope = fit_slope_through_origin(
                        [get_x(row) for row in train_rows],
                        [numeric(row, "routed_power_delta_w") for row in train_rows],
                    )
                    slopes[held_out] = slope
                    held_predictions.extend(slope * get_x(row) for row in test_rows)
                oof_mae = mae(y, held_predictions)
                if feature in {"placement_power_prediction", "placement_wire_cap_power_proxy", "placement_load_cap_power_proxy"}:
                    raw_mae = mae(y, x)
                    lomo_lines.append(
                        f"- {feature}: zero-model MAE {zero_mae * 1e9:.4f} nW/net; raw-feature MAE "
                        f"{raw_mae * 1e9:.4f} nW/net; leave-one-move-out fitted-scale MAE "
                        f"{oof_mae * 1e9:.4f} nW/net."
                    )
                else:
                    lomo_lines.append(
                        f"- {feature}: zero-model MAE {zero_mae * 1e9:.4f} nW/net; leave-one-move-out "
                        f"fitted-scale MAE {oof_mae * 1e9:.4f} nW/net; fitted scales "
                        f"{', '.join(f'{name}={value:.3g}' for name, value in slopes.items())}."
                    )

        # Compare the most direct geometry proxy (placement cap delta) with routed wire-cap delta.
        cap_feature = [numeric(row, "delta_place_wire_cap_f") for row in common]
        add_metric("placement_wire_cap_delta", "routed_delta_wire_cap_f", cap_feature, actual_wire,
                   "all common-signal net/workload rows")
        if physical_columns:
            route_length = [numeric(row, "route_delta_centerline_um") for row in common]
            route_vias = [numeric(row, "route_delta_vias") for row in common]
            placement_abs_hpwl = [numeric(row, "place_abs_delta_hpwl_um") for row in common]
            abs_wire = [abs(value) for value in actual_wire]
            abs_power = [abs(value) for value in actual_power]
            add_metric("routed_centerline_delta_um", "routed_delta_wire_cap_f", route_length, actual_wire,
                       "all common-signal net/workload rows")
            add_metric("abs_routed_centerline_delta_um", "abs_routed_delta_wire_cap_f",
                       [abs(value) for value in route_length], abs_wire, "all common-signal net/workload rows")
            add_metric("routed_via_delta", "routed_delta_wire_cap_f", route_vias, actual_wire,
                       "all common-signal net/workload rows")
            add_metric("placement_abs_hpwl_delta_um", "abs_routed_power_delta_w",
                       placement_abs_hpwl, abs_power, "all common-signal net/workload rows")
            moved = [index for index, value in enumerate(route_length) if value != 0.0]
            if len(moved) >= 2:
                add_metric("routed_centerline_delta_um", "routed_delta_wire_cap_f",
                           [route_length[i] for i in moved], [actual_wire[i] for i in moved],
                           "nets with nonzero routed centerline delta")
                add_metric("abs_routed_centerline_delta_um", "abs_routed_delta_wire_cap_f",
                           [abs(route_length[i]) for i in moved], [abs_wire[i] for i in moved],
                           "nets with nonzero routed centerline delta")
            for column in physical_columns:
                if not column.startswith("route_delta_") or not column.endswith("_um") or column == "route_delta_centerline_um":
                    continue
                layer_feature = [numeric(row, column) for row in common]
                add_metric(column.removesuffix("_um"), "routed_delta_wire_cap_f", layer_feature, actual_wire,
                           "all common-signal net/workload rows")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        dataset_path = args.output_dir / "pooled_per_net_features.csv"
        output_dataset(dataset_path, common, physical_columns)
        metrics_path = args.output_dir / "feature_correlations.tsv"
        with metrics_path.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=["feature", "target", "scope", "rows", "pearson_r", "spearman_rho"], delimiter="\t")
            writer.writeheader()
            writer.writerows(metric_rows)

        lines = [
            "Placement-to-SPEF per-net feature analysis",
            f"Candidates: {', '.join(labels)}",
            f"Matched common-signal rows: {len(common)} ({len(labels)} moves × 3 workloads × 8,584 nets).",
            f"Post-CTS clock rows kept separate: {len(clock)}.",
            "",
            "Calibration source integrity: PASS; each manifest's before/after hashes matched, and all calibration inputs share the same baseline placement ODB, placement SDC, and routed baseline SPEF.",
            "Feature correlations against routed per-net switching-power delta:",
        ]
        for row in metric_rows:
            if row["target"] != "routed_power_delta_w":
                continue
            pearson_text = f"{float(row['pearson_r']):+.4f}" if row["pearson_r"] != "" else "undefined"
            spearman_text = f"{float(row['spearman_rho']):+.4f}" if row["spearman_rho"] != "" else "undefined"
            lines.append(f"- {row['feature']} ({row['scope']}): Pearson r={pearson_text}; Spearman rho={spearman_text}.")
        wire_metric = next(row for row in metric_rows if row["target"] == "routed_delta_wire_cap_f")
        pearson_text = f"{float(wire_metric['pearson_r']):+.4f}" if wire_metric["pearson_r"] != "" else "undefined"
        spearman_text = f"{float(wire_metric['spearman_rho']):+.4f}" if wire_metric["spearman_rho"] != "" else "undefined"
        lines.extend([
            f"- placement_wire_cap_delta vs routed_delta_wire_cap: Pearson r={pearson_text}; Spearman rho={spearman_text}.",
            "",
            "Leave-one-move-out diagnostics (one global scale fit on the other moves; per-net MAE):",
            *lomo_lines,
            "",
            "Move/workload coverage and per-net reconciliation:",
        ])
        for label in labels:
            for workload in WORKLOADS:
                summary = candidate_summaries[label][workload]
                place_error = summary["place_pernet"] - summary["place_reported"]
                route_error = summary["route_pernet"] - summary["route_reported"]
                lines.append(
                    f"- {label} / {workload}: placement residual {place_error * 1e9:+.3f} nW; "
                    f"routed residual {route_error * 1e9:+.3f} nW."
                )
        changed_place = sum(abs(numeric(row, "place_predicted_power_delta_w")) > EPSILON for row in common)
        changed_route = sum(abs(numeric(row, "routed_power_delta_w")) > EPSILON for row in common)
        activity_effect = sum(abs(numeric(row, "routed_activity_power_delta_w")) for row in common)
        if physical_columns:
            unique_physical = {(row["candidate"], row["net"]): row for row in common}
            physical_rows = list(unique_physical.values())
            hpwl_changed = sum(numeric(row, "place_delta_hpwl_um") != 0.0 for row in physical_rows)
            fanout_changed = sum(numeric(row, "place_delta_fanout") != 0.0 for row in physical_rows)
            route_length_changed = sum(numeric(row, "route_delta_centerline_um") != 0.0 for row in physical_rows)
            vias_changed = sum(numeric(row, "route_delta_vias") != 0.0 for row in physical_rows)
            abs_hpwl_change = sum(numeric(row, "place_abs_delta_hpwl_um") for row in physical_rows)
            signed_route_change = sum(numeric(row, "route_delta_centerline_um") for row in physical_rows)
            abs_route_change = sum(numeric(row, "route_abs_delta_centerline_um") for row in physical_rows)
            lines.extend([
                f"Physical features: PASS; joined all {len(common)} common-signal rows to baseline/candidate placement and final-route ODBs; each ODB hash was rechecked.",
                f"Unique candidate/net pairs: {len(physical_rows)}; placement HPWL changed on {hpwl_changed} pairs ({abs_hpwl_change:.3f} µm total absolute change); fanout changed on {fanout_changed} pairs.",
                f"Final route centerline changed on {route_length_changed} pairs; vias changed on {vias_changed}; net-summed route length delta {signed_route_change:+.3f} µm and sum of absolute deltas {abs_route_change:.3f} µm.",
                "Routed centerline length and via counts are post-route explanatory features, not inputs available to a pre-route placer. Length uses Manhattan decoder points and excludes endpoint extensions; it does not model wire width, coupling, or via capacitance.",
            ])
            lines.append("Physical feature correlations are in feature_correlations.tsv; the changed-route subset avoids treating unchanged nets as geometric observations.")
        lines.extend([
            "",
            f"Meaningful placement per-net predictions (> {EPSILON:.0e} W): {changed_place}/{len(common)} rows.",
            f"Meaningful routed per-net power changes (> {EPSILON:.0e} W): {changed_route}/{len(common)} rows.",
            f"Sum of absolute routed activity-attribution contributions: {activity_effect * 1e9:.6f} nW across pooled rows.",
            f"These correlations and leave-one-move-out scales are diagnostics only. The {len(labels)} related move samples are exploratory and do not establish a generalizable correction model.",
            "",
            f"Pooled dataset: {dataset_path}",
            f"Correlation table: {metrics_path}",
        ])
        report_path = args.output_dir / "summary.txt"
        report_path.write_text("\n".join(lines) + "\n")
        print(report_path.read_text(), end="")
        return 0
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(f"feature analysis failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
