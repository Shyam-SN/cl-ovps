#!/usr/bin/env python3
"""Export placement-time leaf-clock access and neighborhood features.

The input is a trial-CTS ODB: its clock-tree cell placements and exact leaf
assignments are used, while no global/detail-route geometry or SPEF is read.
VCD pair metrics are timestamp-level activity proxies, not extracted coupling
capacitance or a signoff power estimate.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
TCL = ROOT / "scripts/export_gcd_cts_spatial_geometry.tcl"
DEFAULT_EXPERIMENT = ROOT / "build/power/gcd_flop_relocation_random_local_20261030a"
PLATFORM_LEF = ROOT / "tools/openroad-flow-scripts/flow/platforms/sky130hd/lef/sky130_fd_sc_hd.tlef"
WORKLOADS = ("idle", "typical", "peak")
LAYERS = ("li1", "mcon", "met1", "met2")
WINDOWS_UM = (0.5, 1.5)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def canonical_name(name: str) -> str:
    return name.replace(r"\[", "[").replace(r"\]", "]")


def parse_vcd(path: Path) -> dict[str, object]:
    aliases: dict[str, set[str]] = defaultdict(set)
    widths: dict[str, int] = {}
    events: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    previous: dict[str, str] = {}
    scopes: list[str] = []
    active = False
    timestamp = 0
    max_timestamp = 0
    timescale = re.search(r"\$timescale\s+(\d+)\s*(s|ms|us|ns|ps|fs)\s+\$end", path.read_text(errors="replace")[:100_000], re.I)
    if not timescale:
        raise ValueError(f"could not parse VCD timescale in {path}")
    scale = int(timescale.group(1)) * {
        "s": 1.0,
        "ms": 1e-3,
        "us": 1e-6,
        "ns": 1e-9,
        "ps": 1e-12,
        "fs": 1e-15,
    }[timescale.group(2).lower()]

    with path.open(errors="replace") as stream:
        for raw in stream:
            line = raw.strip()
            if line.startswith("$scope "):
                fields = line.split()
                if len(fields) >= 3:
                    scopes.append(fields[2])
                continue
            if line.startswith("$upscope"):
                if scopes:
                    scopes.pop()
                continue
            if line.startswith("$var "):
                fields = line.split()
                if len(fields) >= 6:
                    width, code, reference = int(fields[2]), fields[3], fields[4]
                    aliases[canonical_name(reference)].add(code)
                    aliases[".".join(scopes + [canonical_name(reference)])].add(code)
                    widths[code] = width
                continue
            if line.startswith("$enddefinitions"):
                active = True
                continue
            if not active:
                continue
            if line.startswith("#"):
                try:
                    timestamp = int(line[1:])
                    max_timestamp = max(max_timestamp, timestamp)
                except ValueError:
                    pass
                continue
            if not line or line[0] in "$*":
                continue
            if line[0] in "01xXzZ":
                value, code = line[0].lower(), line[1:].strip()
            elif line[0] in "bB":
                fields = line.split()
                if len(fields) != 2:
                    continue
                value, code = fields[0][1:].lower(), fields[1]
            else:
                continue
            if widths.get(code) != 1 or value not in {"0", "1"}:
                previous[code] = value
                continue
            old = previous.get(code)
            if old in {"0", "1"} and old != value:
                events[code][timestamp].append(1 if value == "1" else -1)
            previous[code] = value

    return {
        "aliases": aliases,
        "events": events,
        "duration_s": max_timestamp * scale,
        "timescale_s": scale,
    }


def select_vcd_code(vcd: dict[str, object], net: str) -> str | None:
    aliases = vcd["aliases"]
    events = vcd["events"]
    assert isinstance(aliases, dict) and isinstance(events, dict)
    candidates = set(aliases.get(canonical_name(net), set()))
    if not candidates:
        # OpenDB bus names can include escaped brackets while VCD may expose
        # either the unescaped spelling or a hierarchical alias.
        target = canonical_name(net)
        for alias, codes in aliases.items():
            if alias.split(".")[-1] == target:
                candidates.update(codes)
    if not candidates:
        return None
    return max(candidates, key=lambda code: sum(len(v) for v in events.get(code, {}).values()))


def vcd_net_activity(vcd: dict[str, object], net: str) -> tuple[float | None, str | None]:
    code = select_vcd_code(vcd, net)
    if code is None:
        return None, None
    events = vcd["events"]
    duration_s = float(vcd["duration_s"])
    if duration_s <= 0:
        return None, code
    return sum(len(values) for values in events.get(code, {}).values()) / duration_s, code


def vcd_pair_activity(vcd: dict[str, object], clock_net: str, signal_net: str) -> dict[str, float | None]:
    clock_code = select_vcd_code(vcd, clock_net)
    signal_code = select_vcd_code(vcd, signal_net)
    duration_s = float(vcd["duration_s"])
    empty = {
        "pair_relative_transition_score_hz": None,
        "pair_same_direction_hz": None,
        "pair_opposite_direction_hz": None,
        "pair_unpaired_toggle_hz": None,
    }
    if clock_code is None or signal_code is None or duration_s <= 0:
        return empty
    events = vcd["events"]
    clock_events = events.get(clock_code, {})
    signal_events = events.get(signal_code, {})
    all_times = set(clock_events) | set(signal_events)
    relative_score = same = opposite = unpaired = 0
    for time in all_times:
        clk = clock_events.get(time, [])
        sig = signal_events.get(time, [])
        paired = min(len(clk), len(sig))
        for index in range(paired):
            delta = sig[index] - clk[index]
            relative_score += delta * delta
            if sig[index] == clk[index]:
                same += 1
            else:
                opposite += 1
        extra = abs(len(clk) - len(sig))
        relative_score += extra
        unpaired += extra
    return {
        "pair_relative_transition_score_hz": relative_score / duration_s,
        "pair_same_direction_hz": same / duration_s,
        "pair_opposite_direction_hz": opposite / duration_s,
        "pair_unpaired_toggle_hz": unpaired / duration_s,
    }


def parse_via_rectangles(path: Path, via_name: str) -> dict[str, tuple[float, float, float, float]]:
    lines = path.read_text(errors="replace").splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(rf"\s*VIA\s+{re.escape(via_name)}\b", line)), None)
    if start is None:
        raise ValueError(f"via {via_name} not found in {path}")
    current_layer = ""
    rectangles: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    for line in lines[start + 1 :]:
        if re.match(rf"\s*END\s+{re.escape(via_name)}\b", line):
            break
        layer = re.match(r"\s*LAYER\s+(\S+)\s*;", line)
        rect = re.match(r"\s*RECT\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s+([-+0-9.eE]+)\s*;", line)
        if layer:
            current_layer = layer.group(1)
        elif rect and current_layer:
            rectangles[current_layer].append(tuple(map(float, rect.groups())))
    result = {}
    for layer, shapes in rectangles.items():
        result[layer] = (
            min(shape[0] for shape in shapes),
            min(shape[1] for shape in shapes),
            max(shape[2] for shape in shapes),
            max(shape[3] for shape in shapes),
        )
    for required in ("li1", "mcon", "met1"):
        if required not in result:
            raise ValueError(f"via {via_name} has no {required} rectangle in {path}")
    return result


def clipped(rect: tuple[int, int, int, int], window: tuple[int, int, int, int]) -> tuple[int, int, int, int] | None:
    x1, y1, x2, y2 = rect
    wx1, wy1, wx2, wy2 = window
    out = (max(x1, wx1), max(y1, wy1), min(x2, wx2), min(y2, wy2))
    return out if out[0] < out[2] and out[1] < out[3] else None


def union_area(rectangles: Iterable[tuple[int, int, int, int]]) -> int:
    rects = [r for r in rectangles if r[0] < r[2] and r[1] < r[3]]
    if not rects:
        return 0
    xs = sorted({x for rect in rects for x in (rect[0], rect[2])})
    area = 0
    for x1, x2 in zip(xs, xs[1:]):
        if x1 == x2:
            continue
        intervals = sorted((r[1], r[3]) for r in rects if r[0] < x2 and r[2] > x1)
        if not intervals:
            continue
        ylo, yhi = intervals[0]
        covered = 0
        for lo, hi in intervals[1:]:
            if lo > yhi:
                covered += yhi - ylo
                ylo, yhi = lo, hi
            else:
                yhi = max(yhi, hi)
        covered += yhi - ylo
        area += (x2 - x1) * covered
    return area


def rect_distance_um(a: tuple[int, int, int, int], b: tuple[int, int, int, int], dbu: int) -> float:
    dx = max(0, a[0] - b[2], b[0] - a[2])
    dy = max(0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy) / dbu


def read_raw(path: Path) -> list[dict[str, str]]:
    return read_tsv(path)


def transform_shape_row(row: dict[str, str]) -> dict[str, object]:
    return {
        **row,
        "rect": tuple(int(row[k]) for k in ("xmin_dbu", "ymin_dbu", "xmax_dbu", "ymax_dbu")),
        "layer": row["layer"],
    }


def intersects_line(shape: dict[str, object], axis: str, coord: int, window: tuple[int, int, int, int]) -> bool:
    x1, y1, x2, y2 = shape["rect"]
    wx1, wy1, wx2, wy2 = window
    if axis == "x":
        return x1 <= coord <= x2 and max(y1, wy1) < min(y2, wy2)
    return y1 <= coord <= y2 and max(x1, wx1) < min(x2, wx2)


def is_target_clock_pin(shape: dict[str, object], sink: dict[str, str]) -> bool:
    return (
        shape["kind"] == "pin"
        and shape["instance"] == sink["sink_instance"]
        and shape["pin"] == "CLK"
        and shape["net"] == sink["branch"]
    )


def is_blocker(shape: dict[str, object], sink: dict[str, str]) -> bool:
    if is_target_clock_pin(shape, sink):
        return False
    if shape["kind"] in {"obs", "block_obs"}:
        return True
    if shape["kind"] == "pin":
        # A same-branch clock pin is a legitimate connection point, not a
        # blockage. All other pins occupy routing geometry in the local window.
        return shape["net"] != sink["branch"]
    return False


def local_window_features(
    sink: dict[str, str],
    shapes: list[dict[str, object]],
    tracks: dict[str, dict[str, list[int]]],
    radius_um: float,
    via_extents: dict[str, tuple[float, float, float, float]],
) -> dict[str, object]:
    dbu = int(sink["dbu_per_micron"])
    pin = tuple(int(sink[k]) for k in ("pin_xmin_dbu", "pin_ymin_dbu", "pin_xmax_dbu", "pin_ymax_dbu"))
    radius = int(round(radius_um * dbu))
    window = (pin[0] - radius, pin[1] - radius, pin[2] + radius, pin[3] + radius)
    nearby: list[tuple[dict[str, object], tuple[int, int, int, int]]] = []
    for shape in shapes:
        part = clipped(shape["rect"], window)
        if part:
            nearby.append((shape, part))

    result: dict[str, object] = {
        f"r{radius_um:g}_window_xmin_um": window[0] / dbu,
        f"r{radius_um:g}_window_ymin_um": window[1] / dbu,
        f"r{radius_um:g}_window_xmax_um": window[2] / dbu,
        f"r{radius_um:g}_window_ymax_um": window[3] / dbu,
    }
    categories = {
        "obs": lambda shape: shape["kind"] in {"obs", "block_obs"},
        "signal_pin": lambda shape: shape["kind"] == "pin" and shape["sigtype"] == "SIGNAL" and not is_target_clock_pin(shape, sink),
        "pg_pin": lambda shape: shape["kind"] == "pin" and shape["sigtype"] in {"POWER", "GROUND"},
        "other_clock_pin": lambda shape: shape["kind"] == "pin" and shape["sigtype"] == "CLOCK" and not is_target_clock_pin(shape, sink) and shape["net"] != sink["branch"],
    }
    for category, predicate in categories.items():
        for layer in LAYERS:
            rects = [part for shape, part in nearby if shape["layer"] == layer and predicate(shape)]
            result[f"r{radius_um:g}_{category}_{layer}_area_um2"] = union_area(rects) / (dbu * dbu)
            result[f"r{radius_um:g}_{category}_{layer}_shape_count"] = len(rects)

    neighbor_instances = {
        shape["instance"]
        for shape, _part in nearby
        if shape["instance"] and shape["instance"] != sink["sink_instance"] and shape["kind"] == "pin"
    }
    result[f"r{radius_um:g}_neighbor_pin_instance_count"] = len(neighbor_instances)
    nearby_nets: dict[str, list[tuple[dict[str, object], tuple[int, int, int, int]]]] = defaultdict(list)
    for shape, part in nearby:
        if shape["kind"] == "pin" and shape["sigtype"] == "SIGNAL" and shape["net"]:
            nearby_nets[str(shape["net"])].append((shape, part))
    result[f"r{radius_um:g}_neighbor_signal_net_count"] = len(nearby_nets)

    # Count preferred-direction routing tracks through the window and tracks
    # unobstructed by local LEF OBS, pin, and cut geometry.
    layer_dirs = {"li1": "x", "met1": "y", "met2": "x"}
    for layer, axis in layer_dirs.items():
        if axis == "x":
            lo, hi = window[0], window[2]
        else:
            lo, hi = window[1], window[3]
        coords = [c for c in tracks.get(layer, {}).get(axis, []) if lo <= c <= hi]
        blocker_layers = {layer}
        if layer in {"li1", "met1"}:
            blocker_layers.add("mcon")
        blocked = 0
        for coord in coords:
            if any(
                shape["layer"] in blocker_layers
                and is_blocker(shape, sink)
                and intersects_line(shape, axis, coord, window)
                for shape, _part in nearby
            ):
                blocked += 1
        result[f"r{radius_um:g}_{layer}_preferred_track_count"] = len(coords)
        result[f"r{radius_um:g}_{layer}_free_track_count"] = len(coords) - blocked

    if radius_um == WINDOWS_UM[0]:
        # Candidate L1M1_PR_MR sites are intersections between actual li1 x
        # tracks and met1 y tracks that fall inside the transformed CLK pin.
        li1_x = [c for c in tracks.get("li1", {}).get("x", []) if pin[0] <= c <= pin[2]]
        met1_y = [c for c in tracks.get("met1", {}).get("y", []) if pin[1] <= c <= pin[3]]
        candidate_sites = 0
        clear_sites = 0
        for x in li1_x:
            for y in met1_y:
                candidate_sites += 1
                blocked = False
                for layer, ext in via_extents.items():
                    dx1, dy1, dx2, dy2 = (int(round(v * dbu)) for v in ext)
                    via_rect = (x + dx1, y + dy1, x + dx2, y + dy2)
                    for shape, _part in nearby:
                        if shape["layer"] != layer or not is_blocker(shape, sink):
                            continue
                        if clipped(shape["rect"], via_rect):
                            blocked = True
                            break
                    if blocked:
                        break
                if not blocked:
                    clear_sites += 1
        result["L1M1_PR_MR_track_intersection_sites"] = candidate_sites
        result["L1M1_PR_MR_geometry_clear_sites"] = clear_sites
    return result


def run_candidate(candidate_id: str, odb: Path, raw_dir: Path) -> tuple[Path, Path, Path, str]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    sink_path = raw_dir / f"{candidate_id}.sinks.tsv"
    geometry_path = raw_dir / f"{candidate_id}.geometry.tsv"
    tracks_path = raw_dir / f"{candidate_id}.tracks.tsv"
    log_path = raw_dir / f"{candidate_id}.openroad.log"
    env = os.environ.copy()
    env.update(
        {
            "SPATIAL_ODB": str(odb.resolve()),
            "SPATIAL_CANDIDATE": candidate_id,
            "SPATIAL_SINK_OUT": str(sink_path.resolve()),
            "SPATIAL_GEOMETRY_OUT": str(geometry_path.resolve()),
            "SPATIAL_TRACKS_OUT": str(tracks_path.resolve()),
        }
    )
    completed = subprocess.run(
        [str(OPENROAD), "-exit", str(TCL)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout)
    if completed.returncode != 0 or not all(p.is_file() for p in (sink_path, geometry_path, tracks_path)):
        raise RuntimeError(f"OpenROAD spatial export failed for {candidate_id}; see {log_path}\n{completed.stdout[-3000:]}")
    return sink_path, geometry_path, tracks_path, str(log_path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, default=DEFAULT_EXPERIMENT)
    parser.add_argument("--cts-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--branch-halo-um", type=float, default=2.5)
    parser.add_argument("--candidate", action="append", help="candidate ID to export; repeat to select multiple")
    args = parser.parse_args()
    if args.branch_halo_um <= 0:
        parser.error("--branch-halo-um must be positive")
    experiment = args.experiment_dir.resolve()
    cts_manifest = args.cts_manifest.resolve() if args.cts_manifest else experiment / "analysis/cts_oracle_manifest.tsv"
    output_dir = args.output_dir.resolve() if args.output_dir else experiment / "analysis/placement_spatial_features"
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty spatial feature directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw"
    manifest = [r for r in read_tsv(cts_manifest) if r.get("return_code") == "0" and r.get("status") == "ok"]
    if args.candidate:
        selected = set(args.candidate)
        manifest = [r for r in manifest if r["candidate_id"] in selected]
        missing = selected - {r["candidate_id"] for r in manifest}
        if missing:
            raise ValueError(f"candidate IDs absent from successful CTS manifest: {sorted(missing)}")
    if not manifest:
        raise ValueError(f"no successful CTS ODB rows in {cts_manifest}")

    via_extents = parse_via_rectangles(PLATFORM_LEF, "L1M1_PR_MR")
    vcds = {
        workload: parse_vcd(ROOT / f"build/activity/gcd_baseline20ns/routed/{workload}.vcd")
        for workload in WORKLOADS
    }
    pair_cache: dict[tuple[str, str, str], dict[str, float | None]] = {}
    sink_rows: list[dict[str, object]] = []
    neighbor_rows: list[dict[str, object]] = []
    branch_rows: list[dict[str, object]] = []
    run_rows: list[dict[str, object]] = []

    for index, entry in enumerate(manifest, start=1):
        candidate = entry["candidate_id"]
        odb = Path(entry["cts_odb"]).resolve()
        if not odb.is_file():
            raise FileNotFoundError(f"CTS ODB missing for {candidate}: {odb}")
        print(f"SPATIAL_FEATURE_EXPORT {index}/{len(manifest)} candidate={candidate}", flush=True)
        sink_path, geometry_path, tracks_path, log_path = run_candidate(candidate, odb, raw_dir)
        raw_sinks = read_raw(sink_path)
        shapes = [transform_shape_row(row) for row in read_raw(geometry_path)]
        track_rows = read_raw(tracks_path)
        tracks: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for row in track_rows:
            if row["grid_kind"] == "track":
                tracks[row["layer"]][row["axis"]].append(int(row["coord_dbu"]))
        for layer in tracks:
            for axis in tracks[layer]:
                tracks[layer][axis] = sorted(set(tracks[layer][axis]))

        if not raw_sinks:
            raise ValueError(f"spatial geometry exporter found no sinks for {candidate}")
        dbu = int(raw_sinks[0]["dbu_per_micron"])
        candidate_sinks: list[dict[str, str]] = raw_sinks
        sink_feature_by_branch: dict[str, list[dict[str, object]]] = defaultdict(list)
        for sink in candidate_sinks:
            base = {
                "candidate_id": candidate,
                "branch": sink["branch"],
                "sink_instance": sink["sink_instance"],
                "master": sink["master"],
                "orient": sink["orient"],
                "clock_pin_center_x_um": int(sink["pin_center_x_dbu"]) / dbu,
                "clock_pin_center_y_um": int(sink["pin_center_y_dbu"]) / dbu,
                "clk_pin_bbox_xmin_um": int(sink["pin_xmin_dbu"]) / dbu,
                "clk_pin_bbox_ymin_um": int(sink["pin_ymin_dbu"]) / dbu,
                "clk_pin_bbox_xmax_um": int(sink["pin_xmax_dbu"]) / dbu,
                "clk_pin_bbox_ymax_um": int(sink["pin_ymax_dbu"]) / dbu,
                "branch_halo_um": args.branch_halo_um,
            }
            for radius in WINDOWS_UM:
                base.update(local_window_features(sink, shapes, tracks, radius, via_extents))
            sink_rows.append(base)
            sink_feature_by_branch[sink["branch"]].append(base)

        # A branch corridor is the bounding box of its sink pin rectangles plus
        # an explicit spatial halo. Pre-detail-route CTS ODBs contain no GCell grid.
        for branch, branch_sinks in sorted({b: [s for s in candidate_sinks if s["branch"] == b] for b in {s["branch"] for s in candidate_sinks}}.items()):
            pins = [tuple(int(s[k]) for k in ("pin_xmin_dbu", "pin_ymin_dbu", "pin_xmax_dbu", "pin_ymax_dbu")) for s in branch_sinks]
            halo = int(round(args.branch_halo_um * dbu))
            corridor = (
                min(p[0] for p in pins) - halo,
                min(p[1] for p in pins) - halo,
                max(p[2] for p in pins) + halo,
                max(p[3] for p in pins) + halo,
            )
            nearby_by_net: dict[str, list[dict[str, object]]] = defaultdict(list)
            for shape in shapes:
                if shape["kind"] != "pin" or shape["sigtype"] != "SIGNAL" or not shape["net"]:
                    continue
                part = clipped(shape["rect"], corridor)
                if part:
                    nearby_by_net[str(shape["net"])].append({**shape, "part": part})
            corridor_area_um2 = (corridor[2] - corridor[0]) * (corridor[3] - corridor[1]) / (dbu * dbu)
            pair_proxy_by_workload = {w: 0.0 for w in WORKLOADS}
            activity_sum_by_workload = {w: 0.0 for w in WORKLOADS}
            for net, net_shapes in sorted(nearby_by_net.items()):
                # Ignore the CTS clock itself and any net with no mapped VCD id.
                if net.startswith("clknet_"):
                    continue
                all_rects = [s["part"] for s in net_shapes]
                per_layer_area = {
                    layer: union_area([s["part"] for s in net_shapes if s["layer"] == layer]) / (dbu * dbu)
                    for layer in LAYERS
                }
                pin_area = sum(per_layer_area.values())
                min_distance = min(rect_distance_um(s["rect"], corridor, dbu) for s in net_shapes)
                instance_count = len({s["instance"] for s in net_shapes})
                net_row: dict[str, object] = {
                    "candidate_id": candidate,
                    "branch": branch,
                    "neighbor_net": net,
                    "neighbor_pin_shape_count": len(net_shapes),
                    "neighbor_instance_count": instance_count,
                    "corridor_pin_union_area_um2": pin_area,
                    "corridor_min_pin_distance_um": min_distance,
                    "corridor_area_um2": corridor_area_um2,
                    "proximity_weight": 1.0 / (1.0 + min_distance),
                }
                for layer, area in per_layer_area.items():
                    net_row[f"corridor_pin_{layer}_area_um2"] = area
                for workload in WORKLOADS:
                    vcd = vcds[workload]
                    density, _code = vcd_net_activity(vcd, net)
                    key = (workload, branch, net)
                    pair = pair_cache.get(key)
                    if pair is None:
                        pair = vcd_pair_activity(vcd, branch, net)
                        pair_cache[key] = pair
                    pair_rate = pair["pair_relative_transition_score_hz"]
                    net_row[f"{workload}_neighbor_toggle_density_hz"] = density if density is not None else ""
                    for feature in ("pair_relative_transition_score_hz", "pair_same_direction_hz", "pair_opposite_direction_hz", "pair_unpaired_toggle_hz"):
                        value = pair[feature]
                        net_row[f"{workload}_{feature}"] = value if value is not None else ""
                    if density is not None:
                        activity_sum_by_workload[workload] += density * (pin_area / max(corridor_area_um2, 1e-12)) * net_row["proximity_weight"]
                    if pair_rate is not None:
                        pair_proxy_by_workload[workload] += pair_rate * (pin_area / max(corridor_area_um2, 1e-12)) * net_row["proximity_weight"]
                neighbor_rows.append(net_row)

            branch_feature: dict[str, object] = {
                "candidate_id": candidate,
                "branch": branch,
                "sink_count": len(branch_sinks),
                "corridor_xmin_um": corridor[0] / dbu,
                "corridor_ymin_um": corridor[1] / dbu,
                "corridor_xmax_um": corridor[2] / dbu,
                "corridor_ymax_um": corridor[3] / dbu,
                "corridor_area_um2": corridor_area_um2,
                "branch_halo_um": args.branch_halo_um,
                "neighbor_signal_net_count": len([n for n in nearby_by_net if not n.startswith("clknet_")]),
            }
            sink_feats = sink_feature_by_branch[branch]
            for radius in WINDOWS_UM:
                for feature in (
                    "L1M1_PR_MR_track_intersection_sites",
                    "L1M1_PR_MR_geometry_clear_sites",
                    f"r{radius:g}_li1_free_track_count",
                    f"r{radius:g}_met1_free_track_count",
                    f"r{radius:g}_met2_free_track_count",
                    f"r{radius:g}_obs_li1_area_um2",
                    f"r{radius:g}_obs_mcon_area_um2",
                    f"r{radius:g}_obs_met1_area_um2",
                    f"r{radius:g}_signal_pin_li1_area_um2",
                    f"r{radius:g}_signal_pin_met1_area_um2",
                    f"r{radius:g}_pg_pin_li1_area_um2",
                    f"r{radius:g}_pg_pin_met1_area_um2",
                    f"r{radius:g}_neighbor_signal_net_count",
                ):
                    branch_feature[f"sum_{feature}"] = sum(float(row.get(feature, 0.0)) for row in sink_feats)
            for workload in WORKLOADS:
                branch_feature[f"{workload}_vcd_neighbor_activity_proximity_proxy"] = activity_sum_by_workload[workload]
                branch_feature[f"{workload}_vcd_pair_relative_transition_proximity_proxy"] = pair_proxy_by_workload[workload]
            branch_rows.append(branch_feature)

        run_rows.append(
            {
                "candidate_id": candidate,
                "cts_odb": str(odb),
                "sink_count": len(raw_sinks),
                "geometry_shape_count": len(shapes),
                "track_row_count": len(track_rows),
                "openroad_log": log_path,
            }
        )

    sink_fields = list(sink_rows[0])
    neighbor_fields = list(neighbor_rows[0]) if neighbor_rows else []
    branch_fields = list(branch_rows[0]) if branch_rows else []
    write_tsv(output_dir / "sink_features.tsv", sink_rows, sink_fields)
    write_tsv(output_dir / "branch_neighbor_activity.tsv", neighbor_rows, neighbor_fields)
    write_tsv(output_dir / "branch_features.tsv", branch_rows, branch_fields)
    write_tsv(output_dir / "export_manifest.tsv", run_rows, list(run_rows[0]))

    summary = [
        "# Placement-time CTS spatial feature export",
        "",
        f"Candidates exported: {len(run_rows)}; clock sinks: {sum(int(r['sink_count']) for r in run_rows)}; raw placed geometry shapes: {sum(int(r['geometry_shape_count']) for r in run_rows)}.",
        f"Local access windows: {', '.join(f'{r:g} µm' for r in WINDOWS_UM)} around transformed li1 CLK-pin bounding boxes.",
        f"Branch corridor: sink-pin bounding box plus {args.branch_halo_um:g} µm fixed halo. The CTS-stage ODB has no GCell grid, so this is a declared distance rather than a GCell-derived halo.",
        "",
        "## Feature meaning and limits",
        "",
        "- LEF pin and OBS rectangles are transformed into placed coordinates with each instance's actual orientation. Track coordinates come from the ODB track grids.",
        "- `L1M1_PR_MR_track_intersection_sites` counts li1/met1 track intersections inside a CLK pin. `...geometry_clear_sites` removes sites whose via footprints overlap exported pin/OBS geometry on li1, mcon, or met1. This is a geometry screen, not a complete DRC/legal-via proof.",
        "- Local OBS, signal-pin, PG-pin areas are rectangle-union areas, split by layer. Track-free counts are coarse window features; they do not model router track assignment or special-net shielding.",
        "- Neighbor activity rows use signal-pin geometry inside the branch sink-bbox corridor and VCD-mapped net toggles. `pair_relative_transition_score_hz` pairs transitions that occur at the same VCD timestamp and weights their signed voltage changes by `(ΔV_signal - ΔV_clock)^2`. It distinguishes same-direction from opposite-direction toggles, unlike a toggle-intersection count, but zero-delay/same-timestamp VCD events do not reveal sub-timestep ordering or routed aggressor adjacency.",
        "- Activity-proximity values are features only. They are not predicted capacitance or switching power and have not been fitted against the existing routed holdout.",
        "",
        "Use the existing routed pool only to inspect feature behavior. Freeze a fresh candidate pool and all selected feature/score definitions before generating detailed-route SPEF labels.",
    ]
    (output_dir / "README.md").write_text("\n".join(summary) + "\n")
    print(
        f"GCD_SPATIAL_FEATURES candidates={len(run_rows)} sinks={len(sink_rows)} "
        f"branches={len(branch_rows)} output={output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
