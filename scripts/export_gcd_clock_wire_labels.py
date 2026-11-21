#!/usr/bin/env python3
"""Export per-clock-net post-route labels and geometry for GCD experiments.

This is a diagnostic/training-data exporter. SPEF/DEF labels describe the
completed route and must not be treated as placement-time predictor inputs.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "build/orfs/results/sky130hd/gcd_power"
CLOCK_NETS = (
    "clknet_0_clk",
    "clknet_2_0__leaf_clk",
    "clknet_2_1__leaf_clk",
    "clknet_2_2__leaf_clk",
    "clknet_2_3__leaf_clk",
)
LAYERS = ("li1", "met1", "met2", "met3", "met4", "met5")
VOLTAGE = 1.8


def spef_clock_caps(path: Path) -> dict[str, dict[str, float]]:
    text = path.read_text(errors="replace")
    cap_unit = re.search(r"(?m)^\*C_UNIT\s+([0-9.eE+-]+)\s+(\w+)", text)
    if not cap_unit or cap_unit.group(2).upper() != "PF":
        raise ValueError(f"expected SPEF capacitance units in pF: {path}")
    to_ff = float(cap_unit.group(1)) * 1000.0

    name_map: dict[str, str] = {}
    in_name_map = False
    for line in text.splitlines():
        if line.startswith("*NAME_MAP"):
            in_name_map = True
            continue
        if in_name_map:
            if line.startswith("*PORTS"):
                break
            match = re.match(r"\s*(\*\d+)\s+(\S+)", line)
            if match:
                name_map[match.group(1)] = match.group(2)

    caps: dict[str, dict[str, float]] = {}
    for match in re.finditer(
        r"(?m)^\*D_NET\s+(\S+)\s+([0-9.eE+-]+)\s*\n(.*?)^\*END\s*$",
        text,
        re.S,
    ):
        net = name_map.get(match.group(1), match.group(1))
        if net not in CLOCK_NETS:
            continue
        body = match.group(3)
        if "*CAP" not in body:
            raise ValueError(f"missing *CAP section for {net} in {path}")
        cap_section = body.split("*CAP", 1)[1].split("*RES", 1)[0]
        ground_pf = 0.0
        coupling_pf = 0.0
        for line in cap_section.splitlines():
            fields = line.split()
            if len(fields) == 3:
                try:
                    ground_pf += float(fields[2])
                except ValueError:
                    continue
            elif len(fields) == 4:
                try:
                    coupling_pf += float(fields[3])
                except ValueError:
                    continue
        caps[net] = {
            "declared_wire_ff": float(match.group(2)) * to_ff,
            "ground_ff": ground_pf * to_ff,
            "coupling_ff": coupling_pf * to_ff,
        }
        caps[net]["wire_ff"] = caps[net]["ground_ff"] + caps[net]["coupling_ff"]

    missing = set(CLOCK_NETS) - set(caps)
    if missing:
        raise ValueError(f"missing clock SPEF nets in {path}: {sorted(missing)}")
    return caps


def parse_def_net(text: str, net: str, dbu_per_micron: int) -> dict[str, object]:
    net_section = text.split("NETS ", 1)[1].split("END NETS", 1)[0]
    match = re.search(r"(?m)^\s*-\s+" + re.escape(net) + r"\b(.*?);", net_section, re.S)
    if not match:
        raise ValueError(f"missing DEF clock net {net}")
    body = match.group(1)
    ndr_match = re.search(r"\+\s+NONDEFAULTRULE\s+(\S+)", body)
    ndr = ndr_match.group(1) if ndr_match else "default"
    lengths_dbu = {layer: 0 for layer in LAYERS}
    route_tags = list(re.finditer(r"\b(?:ROUTED|NEW)\s+(li\d+|met\d+)\b", body))
    for i, tag in enumerate(route_tags):
        layer = tag.group(1)
        end = route_tags[i + 1].start() if i + 1 < len(route_tags) else len(body)
        route_part = body[tag.end():end]
        points: list[tuple[int, int]] = []
        previous: list[int | None] = [None, None]
        for point in re.finditer(r"\(\s*(-?\d+|\*)\s+(-?\d+|\*)\s*\)", route_part):
            xy: list[int | None] = []
            for axis, value in enumerate(point.groups()):
                if value != "*":
                    previous[axis] = int(value)
                xy.append(previous[axis])
            if xy[0] is not None and xy[1] is not None:
                points.append((int(xy[0]), int(xy[1])))
        lengths_dbu[layer] += sum(
            abs(x2 - x1) + abs(y2 - y1)
            for (x1, y1), (x2, y2) in zip(points, points[1:])
        )

    # The route records contain the technology-specific via names. Keep the
    # counts by name so each PDK's via stack remains explicit in the data.
    via_counts: dict[str, int] = {}
    for via in re.findall(r"\b(?:L1M1_PR_MR|M1M2_PR|M2M3_PR|M3M4_PR|M4M5_PR|via\d*|mcon)\b", body):
        via_counts[via] = via_counts.get(via, 0) + 1
    return {
        "ndr": ndr,
        "length_um": {layer: value / dbu_per_micron for layer, value in lengths_dbu.items()},
        "via_counts": via_counts,
    }


def guide_features(path: Path, net: str) -> dict[str, dict[str, float]]:
    lines = path.read_text(errors="replace").splitlines()
    features: dict[str, dict[str, float]] = {}
    index = 0
    while index + 1 < len(lines):
        if lines[index].strip() != net or lines[index + 1].strip() != "(":
            index += 1
            continue
        index += 2
        while index < len(lines) and lines[index].strip() != ")":
            fields = lines[index].split()
            if len(fields) == 5:
                try:
                    x1, y1, x2, y2 = map(int, fields[:4])
                except ValueError:
                    index += 1
                    continue
                layer = fields[4]
                values = features.setdefault(layer, {"count": 0.0, "extent_dbu": 0.0, "area_dbu2": 0.0})
                width, height = abs(x2 - x1), abs(y2 - y1)
                values["count"] += 1
                values["extent_dbu"] += width + height
                values["area_dbu2"] += width * height
            index += 1
        break
    return features


def clock_activity(path: Path) -> dict[str, float]:
    result: dict[str, float] = {}
    for line in path.read_text().splitlines():
        fields = line.split("\t")
        if len(fields) == 9 and fields[0] == "#SPEF_DRIVER_CAP" and fields[1] != "pin" and fields[8] == "clock":
            result[fields[2]] = float(fields[6])
    return result


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path("build/power/gcd_flop_relocation_random_local_20261030a"),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    output = args.output.resolve() if args.output else experiment / "analysis/clock_wire_labels.tsv"

    variants = [{"candidate_id": "baseline", "route_variant": "gcd_baseline20ns"}]
    variants.extend(
        {"candidate_id": row["candidate_id"], "route_variant": row["route_variant"]}
        for row in read_manifest(experiment / "route_manifest.tsv")
        if row.get("return_code") == "0"
    )
    collected: list[dict[str, object]] = []
    baseline_caps: dict[str, dict[str, float]] | None = None
    baseline_lengths: dict[str, dict[str, float]] | None = None
    baseline_guides: dict[str, dict[str, dict[str, float]]] | None = None

    for variant in variants:
        candidate = variant["candidate_id"]
        route_variant = variant["route_variant"]
        result_dir = RESULTS / route_variant
        def_path = result_dir / "6_final.def"
        spef_path = result_dir / "6_final.spef"
        guide_path = result_dir / "route.guide"
        if not all(path.is_file() for path in (def_path, spef_path, guide_path)):
            raise FileNotFoundError(f"incomplete routed artifacts for {candidate}: {result_dir}")
        def_text = def_path.read_text(errors="replace")
        units = re.search(r"UNITS DISTANCE MICRONS\s+(\d+)", def_text)
        if not units:
            raise ValueError(f"missing DEF distance units in {def_path}")
        dbu_per_micron = int(units.group(1))
        caps = spef_clock_caps(spef_path)
        activity = None
        if candidate != "baseline":
            activity_path = ROOT / f"build/power/{route_variant}/candidate/idle.driver_caps.tsv"
            activity = clock_activity(activity_path)
        length_map: dict[str, dict[str, float]] = {}
        guide_map: dict[str, dict[str, dict[str, float]]] = {}
        route_map: dict[str, dict[str, object]] = {}
        for net in CLOCK_NETS:
            route = parse_def_net(def_text, net, dbu_per_micron)
            route_map[net] = route
            length_map[net] = route["length_um"]  # type: ignore[assignment]
            guide_map[net] = guide_features(guide_path, net)

        if candidate == "baseline":
            baseline_caps = caps
            baseline_lengths = length_map
            baseline_guides = guide_map

        for net in CLOCK_NETS:
            if baseline_caps is None or baseline_lengths is None or baseline_guides is None:
                raise RuntimeError("baseline must be processed before candidates")
            cap = caps[net]
            base_cap = baseline_caps[net]
            lengths = length_map[net]
            base_lengths = baseline_lengths[net]
            guide = guide_map[net]
            base_guide = baseline_guides[net]
            cap_delta_ff = cap["wire_ff"] - base_cap["wire_ff"]
            density = 0.0 if activity is None else activity.get(net, 0.0)
            row: dict[str, object] = {
                "candidate_id": candidate,
                "route_variant": route_variant,
                "net": net,
                "ndr": route_map[net]["ndr"],
                "spef_wire_ff": cap["wire_ff"],
                "spef_ground_ff": cap["ground_ff"],
                "spef_coupling_ff": cap["coupling_ff"],
                "spef_declared_wire_ff": cap["declared_wire_ff"],
                "delta_wire_ff_vs_baseline": cap_delta_ff,
                "clock_density_hz": density,
                "delta_switching_nw_at_idle_clock_density": 0.5 * VOLTAGE**2 * density * cap_delta_ff * 1e-6,
                "via_counts": ",".join(f"{k}:{v}" for k, v in sorted(route_map[net]["via_counts"].items())),
            }
            for layer in LAYERS:
                row[f"def_{layer}_length_um"] = lengths.get(layer, 0.0)
                row[f"delta_def_{layer}_length_um"] = lengths.get(layer, 0.0) - base_lengths.get(layer, 0.0)
                row[f"guide_{layer}_rect_count"] = guide.get(layer, {}).get("count", 0.0)
                row[f"delta_guide_{layer}_rect_count"] = guide.get(layer, {}).get("count", 0.0) - base_guide.get(layer, {}).get("count", 0.0)
                row[f"guide_{layer}_extent_um"] = guide.get(layer, {}).get("extent_dbu", 0.0) / dbu_per_micron
                row[f"delta_guide_{layer}_extent_um"] = (
                    guide.get(layer, {}).get("extent_dbu", 0.0) - base_guide.get(layer, {}).get("extent_dbu", 0.0)
                ) / dbu_per_micron
                row[f"guide_{layer}_area_um2"] = guide.get(layer, {}).get("area_dbu2", 0.0) / dbu_per_micron**2
            collected.append(row)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=list(collected[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(collected)
    print(f"Wrote {len(collected)} per-net rows for {len(variants)} routed layouts: {output}")
    print("These are post-route labels and route features for diagnosis; do not feed them to a placement-time score.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
