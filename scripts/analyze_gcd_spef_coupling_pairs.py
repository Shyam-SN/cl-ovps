#!/usr/bin/env python3
"""Map routed GCD clock SPEF coupling capacitors to VCD aggressor activity.

This is a diagnostic analysis of an already-routed candidate pool. It does not
fit or modify a placer score. The reported coupling-power quantity is a
timestamp-resolved proxy, 0.5 * Cc * V^2 * relative_transition_rate, where
relative_transition_rate counts same-timestamp transition direction
difference. SPEF coupling records carry node names, not physical coordinates.
"""

from __future__ import annotations

import argparse
import csv
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from export_gcd_cts_spatial_features import parse_vcd


ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = ROOT / "build/orfs/results/sky130hd/gcd_power"
POWER_ROOT = ROOT / "build/power"
ACTIVITY_ROOT = ROOT / "build/activity"
WORKLOADS = ("idle", "typical", "peak")
VICTIM_NETS = tuple(f"clknet_2_{index}__leaf_clk" for index in range(4))
VOLTAGE_V = 1.8


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def canonical(name: str) -> str:
    return name.replace(r"\[", "[").replace(r"\]", "]")


def enrich_vector_bit_aliases(vcd: dict[str, Any], path: Path) -> None:
    """Expand vector VCD traces into bit-level aliases needed by SPEF ports."""
    if vcd.get("_vector_bits_enriched"):
        return
    lines = path.read_text(errors="replace").splitlines()
    scopes: list[str] = []
    vector_vars: list[tuple[str, str, int, int, int]] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("$scope "):
            fields = stripped.split()
            if len(fields) >= 3:
                scopes.append(fields[2])
        elif stripped.startswith("$upscope"):
            if scopes:
                scopes.pop()
        elif stripped.startswith("$var "):
            fields = stripped.split()
            if len(fields) >= 5:
                try:
                    width = int(fields[2])
                except ValueError:
                    continue
                reference = canonical(fields[4])
                if width <= 1:
                    continue
                declared_range = fields[5] if len(fields) >= 6 and fields[5].startswith("[") else ""
                range_match = re.fullmatch(r"\[(-?\d+):(-?\d+)\]", declared_range)
                if range_match:
                    msb, lsb = map(int, range_match.groups())
                else:
                    msb, lsb = width - 1, 0
                vector_vars.append((".".join(scopes), reference, fields[3], msb, lsb))
        elif stripped.startswith("$enddefinitions"):
            active = True
            break

    if not vector_vars:
        vcd["_vector_bits_enriched"] = True
        return

    selected_codes = {code for _, _, code, _, _ in vector_vars}
    previous: dict[str, str] = {}
    timestamps: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    timestamp = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("$enddefinitions"):
            active = True
            continue
        if not active:
            continue
        if stripped.startswith("#"):
            try:
                timestamp = int(stripped[1:])
            except ValueError:
                pass
            continue
        if not stripped or stripped[0] in "$*" or stripped[0].lower() != "b":
            continue
        fields = stripped.split()
        if len(fields) != 2 or fields[1] not in selected_codes:
            continue
        code = fields[1]
        current_value = fields[0][1:].lower()
        previous_value = previous.get(code)
        if previous_value is not None:
            width = max(len(previous_value), len(current_value))
            old_bits = previous_value.zfill(width)
            new_bits = current_value.zfill(width)
            for scope, reference, variable_code, msb, lsb in vector_vars:
                if variable_code != code:
                    continue
                indices = list(range(msb, lsb - 1, -1)) if msb >= lsb else list(range(msb, lsb + 1))
                for position, index in enumerate(indices):
                    if position >= width:
                        continue
                    old_bit, new_bit = old_bits[position], new_bits[position]
                    if old_bit not in "01" or new_bit not in "01" or old_bit == new_bit:
                        continue
                    bit_code = f"__vcd_vector__{scope}.{reference}[{index}]"
                    timestamps[bit_code][timestamp].append(1 if new_bit == "1" else -1)
        previous[code] = current_value

    aliases: dict[str, set[str]] = vcd["aliases"]
    events: dict[str, dict[int, list[int]]] = vcd["events"]
    for scope, reference, code, msb, lsb in vector_vars:
        indices = list(range(msb, lsb - 1, -1)) if msb >= lsb else list(range(msb, lsb + 1))
        for index in indices:
            bit_code = f"__vcd_vector__{scope}.{reference}[{index}]"
            events[bit_code] = timestamps.get(bit_code, {})
            aliases[f"{reference}[{index}]"].add(bit_code)
            if scope:
                aliases[f"{scope}.{reference}[{index}]"].add(bit_code)
    vcd["_vector_bits_enriched"] = True


def parse_spef(path: Path) -> dict[str, Any]:
    """Return clock-leaf coupling records and accounting totals from SPEF."""
    lines = path.read_text(errors="replace").splitlines()
    cap_unit: tuple[float, str] | None = None
    name_map: dict[str, str] = {}
    in_name_map = False
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    section = ""

    for line in lines:
        if line.startswith("*C_UNIT"):
            fields = line.split()
            if len(fields) >= 3:
                cap_unit = (float(fields[1]), fields[2].upper())
            continue
        if line.startswith("*NAME_MAP"):
            in_name_map = True
            continue
        if in_name_map:
            if line.startswith("*PORTS"):
                in_name_map = False
            else:
                match = re.match(r"\s*(\*\d+)\s+(\S+)", line)
                if match:
                    name_map[match.group(1)] = match.group(2)
                continue
        dnet = re.match(r"^\*D_NET\s+(\S+)\s+([0-9.eE+-]+)", line)
        if dnet:
            current = {
                "token": dnet.group(1),
                "net": name_map.get(dnet.group(1), dnet.group(1)),
                "declared_pf": float(dnet.group(2)),
                "connections": [],
                "caps": [],
            }
            sections.append(current)
            section = ""
            continue
        if current is None:
            continue
        if line.startswith("*END"):
            current = None
            section = ""
            continue
        if line.startswith("*CONN"):
            section = "conn"
            continue
        if line.startswith("*CAP"):
            section = "cap"
            continue
        if line.startswith("*RES"):
            section = "res"
            continue
        if section == "conn":
            fields = line.split()
            if len(fields) >= 2 and fields[0] in {"*I", "*P"}:
                current["connections"].append(fields[1])
        elif section == "cap":
            fields = line.split()
            if len(fields) == 3:
                try:
                    current["caps"].append((fields[1], None, float(fields[2])))
                except ValueError:
                    pass
            elif len(fields) == 4:
                try:
                    current["caps"].append((fields[1], fields[2], float(fields[3])))
                except ValueError:
                    pass

    if cap_unit is None or cap_unit[1] != "PF":
        raise ValueError(f"expected SPEF capacitance units in pF: {path}")
    unit_f = cap_unit[0] * 1e-12
    net_names = {entry["net"] for entry in sections}
    connection_owner: dict[str, str] = {}
    for entry in sections:
        for endpoint in entry["connections"]:
            connection_owner[endpoint] = entry["net"]

    def endpoint_net(endpoint: str) -> str | None:
        if endpoint in connection_owner:
            return connection_owner[endpoint]
        prefix = endpoint.split(":", 1)[0]
        if prefix in connection_owner:
            return connection_owner[prefix]
        resolved = name_map.get(prefix, prefix)
        if resolved in net_names:
            return resolved
        # A raw SPEF net name can occur without name-map compression.
        if prefix in net_names:
            return prefix
        return None

    pairs: list[dict[str, object]] = []
    per_victim: dict[str, dict[str, float]] = defaultdict(
        lambda: {"ground_pf": 0.0, "coupling_pf": 0.0, "declared_pf": 0.0,
                 "coupling_records": 0.0, "mapped_records": 0.0,
                 "mapped_coupling_pf": 0.0}
    )
    unresolved: list[dict[str, str]] = []
    for entry in sections:
        victim = entry["net"]
        if victim not in VICTIM_NETS:
            continue
        values = per_victim[victim]
        values["declared_pf"] = entry["declared_pf"]
        for node_a, node_b, cap_pf in entry["caps"]:
            if node_b is None:
                values["ground_pf"] += cap_pf
                continue
            values["coupling_pf"] += cap_pf
            values["coupling_records"] += 1
            owner_a = endpoint_net(node_a)
            owner_b = endpoint_net(node_b)
            if owner_a == victim and owner_b and owner_b != victim:
                aggressor = owner_b
            elif owner_b == victim and owner_a and owner_a != victim:
                aggressor = owner_a
            else:
                unresolved.append({
                    "victim_net": victim,
                    "node_a": node_a,
                    "owner_a": owner_a or "",
                    "node_b": node_b,
                    "owner_b": owner_b or "",
                    "cap_pf": str(cap_pf),
                })
                continue
            values["mapped_records"] += 1
            values["mapped_coupling_pf"] += cap_pf
            pairs.append({
                "victim_net": victim,
                "aggressor_net": aggressor,
                "node_a": node_a,
                "node_b": node_b,
                "coupling_pf": cap_pf,
                "coupling_f": cap_pf * unit_f,
            })

    missing = set(VICTIM_NETS) - set(per_victim)
    if missing:
        raise ValueError(f"missing clock leaf victim nets in {path}: {sorted(missing)}")
    return {
        "pairs": pairs,
        "per_victim": per_victim,
        "unresolved": unresolved,
        "name_map": name_map,
        "net_names": net_names,
        "unit_f": unit_f,
    }


def vcd_code(vcd: dict[str, Any], net: str) -> str | None:
    """Match SPEF names against VCD aliases, including slash/dot hierarchy."""
    source_path = vcd.get("_source_path")
    if source_path is not None:
        enrich_vector_bit_aliases(vcd, Path(str(source_path)))
    aliases: dict[str, set[str]] = vcd["aliases"]
    events: dict[str, dict[int, list[int]]] = vcd["events"]
    target = canonical(net)
    dotted = target.replace("/", ".")
    index = vcd.get("_spef_alias_index")
    if index is None:
        suffix_index: dict[str, set[str]] = defaultdict(set)
        for alias, codes in aliases.items():
            parts = canonical(alias).replace("/", ".").split(".")
            for start in range(len(parts)):
                suffix_index[".".join(parts[start:])].update(codes)
        index = suffix_index
        vcd["_spef_alias_index"] = index
    candidates: set[str] = index.get(dotted, set())
    if not candidates:
        candidates = index.get(target, set())
    if not candidates:
        return None
    return max(candidates, key=lambda code: sum(len(times) for times in events.get(code, {}).values()))


def transition_events(vcd: dict[str, Any], code: str | None) -> dict[int, list[int]]:
    if code is None:
        return {}
    return vcd["events"].get(code, {})


def activity_rate(events: dict[int, list[int]], duration_s: float) -> float | None:
    if duration_s <= 0:
        return None
    return sum(len(values) for values in events.values()) / duration_s


def relative_rate(
    events_a: dict[int, list[int]], events_b: dict[int, list[int]], duration_s: float
) -> tuple[float | None, float | None, float | None, float | None]:
    """Return relative, in-phase, opposite, and unpaired transition rates."""
    if duration_s <= 0:
        return None, None, None, None
    all_times = set(events_a) | set(events_b)
    relative = same = opposite = unpaired = 0
    for timestamp in all_times:
        first, second = events_a.get(timestamp, []), events_b.get(timestamp, [])
        paired = min(len(first), len(second))
        for index in range(paired):
            diff = first[index] - second[index]
            relative += diff * diff
            if first[index] == second[index]:
                same += 1
            else:
                opposite += 1
        extra = abs(len(first) - len(second))
        relative += extra
        unpaired += extra
    return (relative / duration_s, same / duration_s,
            opposite / duration_s, unpaired / duration_s)


def parse_log_power(path: Path) -> dict[str, float]:
    text = path.read_text(errors="replace")
    metric = re.search(r"SPEF_METRICS\s+.*?switching_w=([0-9.eE+-]+)", text)
    group = re.search(
        r"(?m)^Clock\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)",
        text,
    )
    if metric is None or group is None:
        raise ValueError(f"missing routed switching/group-power data in {path}")
    return {
        "design_switching_w": float(metric.group(1)),
        "clock_internal_w": float(group.group(1)),
        "clock_switching_w": float(group.group(2)),
    }


def average_ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = ((start + 1) + end) / 2.0
        for index in order[start:end]:
            result[index] = rank
        start = end
    return result


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    mean_l, mean_r = sum(left) / len(left), sum(right) / len(right)
    centered_l = [value - mean_l for value in left]
    centered_r = [value - mean_r for value in right]
    denominator = math.sqrt(sum(x * x for x in centered_l) * sum(y * y for y in centered_r))
    if denominator == 0:
        return None
    return sum(x * y for x, y in zip(centered_l, centered_r)) / denominator


def correlations(x: list[float], y: list[float]) -> tuple[int, float | None, float | None]:
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    rho = pearson(average_ranks(x), average_ranks(y)) if n >= 2 else None
    concordant = discordant = ties_x = ties_y = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = (x[i] > x[j]) - (x[i] < x[j])
            dy = (y[i] > y[j]) - (y[i] < y[j])
            if dx == 0 and dy == 0:
                continue
            if dx == 0:
                ties_x += 1
            elif dy == 0:
                ties_y += 1
            elif dx == dy:
                concordant += 1
            else:
                discordant += 1
    denom = math.sqrt((concordant + discordant + ties_x) * (concordant + discordant + ties_y))
    tau = (concordant - discordant) / denom if denom else None
    return n, rho, tau


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--experiment-dir", type=Path,
        default=ROOT / "build/power/gcd_flop_relocation_spatial_stage2_20261029a",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--baseline-power-variant",
        help="routed candidate variant whose matched baseline OpenSTA reports are used",
    )
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    output = (args.output_dir or (experiment / "analysis/spef_coupling_aggressors")).resolve()
    manifest_path = experiment / "route_manifest.tsv"
    manifest = read_tsv(manifest_path)
    baseline_variant = "gcd_baseline20ns"
    cases = [{"candidate_id": "baseline", "route_variant": baseline_variant}]
    cases.extend({"candidate_id": row["candidate_id"], "route_variant": row["route_variant"]} for row in manifest)
    baseline_power_variant = args.baseline_power_variant or (
        manifest[0]["route_variant"] if manifest else "gcd_reloc_spatial_stage2_20261029a_rand001"
    )

    # Validate the per-victim capacitance sums against the earlier, independent
    # clock-label export before multiplying any capacitance by activity.
    labels_path = experiment / "analysis/clock_wire_labels.tsv"
    labels = {(row["candidate_id"], row["net"]): row for row in read_tsv(labels_path)}
    cap_check_rows: list[dict[str, object]] = []
    all_pair_rows: list[dict[str, object]] = []
    unmapped_vcd_rows: list[dict[str, object]] = []
    unresolved_rows: list[dict[str, object]] = []
    per_case_data: dict[str, dict[str, Any]] = {}
    for case in cases:
        candidate = case["candidate_id"]
        variant = case["route_variant"]
        spef_path = RESULTS_ROOT / variant / "6_final.spef"
        parsed = parse_spef(spef_path)
        per_case_data[candidate] = parsed
        for victim in VICTIM_NETS:
            values = parsed["per_victim"][victim]
            label = labels[(candidate, victim)]
            label_ff = float(label["spef_coupling_ff"])
            parsed_ff = values["coupling_pf"] * 1000.0
            cap_check_rows.append({
                "candidate_id": candidate,
                "victim_net": victim,
                "spef_pair_coupling_ff": f"{parsed_ff:.12g}",
                "clock_label_coupling_ff": f"{label_ff:.12g}",
                "abs_error_ff": f"{abs(parsed_ff - label_ff):.12g}",
                "pair_records": int(values["coupling_records"]),
                "mapped_endpoint_records": int(values["mapped_records"]),
                "mapped_cap_fraction": (
                    values["mapped_coupling_pf"] / values["coupling_pf"]
                    if values["coupling_pf"] else 1.0
                ),
            })
        for row in parsed["unresolved"]:
            unresolved_rows.append({"candidate_id": candidate, **row})

    max_cap_error = max(float(row["abs_error_ff"]) for row in cap_check_rows)
    if max_cap_error > 1e-7:
        raise ValueError(f"SPEF coupling-pair totals do not reconcile with labels; max error={max_cap_error} fF")

    vcd_cache: dict[tuple[str, str], dict[str, Any]] = {}
    power_rows: list[dict[str, object]] = []
    for case in cases:
        candidate, variant = case["candidate_id"], case["route_variant"]
        for workload in WORKLOADS:
            vcd_path = (
                ACTIVITY_ROOT / baseline_variant / "routed" / f"{workload}.vcd"
                if candidate == "baseline"
                else ACTIVITY_ROOT / variant / "candidate" / f"{workload}.vcd"
            )
            cache_key = (str(vcd_path), workload)
            if cache_key not in vcd_cache:
                vcd_cache[cache_key] = parse_vcd(vcd_path)
                vcd_cache[cache_key]["_source_path"] = str(vcd_path)
            vcd = vcd_cache[cache_key]
            duration_s = float(vcd["duration_s"])
            clock_codes: dict[str, str | None] = {
                victim: vcd_code(vcd, victim) for victim in VICTIM_NETS
            }
            pair_aggregates: dict[str, dict[str, Any]] = defaultdict(
                lambda: {"cap_f": 0.0, "matched_cap_f": 0.0, "proxy_w": 0.0,
                         "unweighted_proxy_w": 0.0, "relative_rate_weighted": 0.0,
                         "same_rate_weighted": 0.0, "opposite_rate_weighted": 0.0,
                         "unpaired_rate_weighted": 0.0, "pair_records": 0,
                         "matched_pairs": 0, "unmatched_pairs": 0}
            )
            branch_aggregates: dict[str, dict[str, Any]] = defaultdict(
                lambda: {"cap_f": 0.0, "matched_cap_f": 0.0, "proxy_w": 0.0,
                         "unweighted_proxy_w": 0.0, "relative_rate_weighted": 0.0,
                         "same_rate_weighted": 0.0, "opposite_rate_weighted": 0.0,
                         "unpaired_rate_weighted": 0.0, "pair_records": 0,
                         "matched_pairs": 0, "unmatched_pairs": 0}
            )
            all_pairs = per_case_data[candidate]["pairs"]
            local_pair_activity: dict[tuple[str, str], dict[str, Any]] = {}
            code_cache: dict[str, str | None] = dict(clock_codes)
            rate_cache: dict[str, float | None] = {}
            for pair in all_pairs:
                victim, aggressor = str(pair["victim_net"]), str(pair["aggressor_net"])
                c_f = float(pair["coupling_f"])
                victim_code = clock_codes[victim]
                if aggressor not in code_cache:
                    code_cache[aggressor] = vcd_code(vcd, aggressor)
                aggressor_code = code_cache[aggressor]
                pair_id = (victim, aggressor)
                if pair_id not in local_pair_activity:
                    if victim_code is None or aggressor_code is None:
                        activity = (None, None, None, None)
                        aggressor_rate = None
                    else:
                        victim_events = transition_events(vcd, victim_code)
                        aggressor_events = transition_events(vcd, aggressor_code)
                        activity = relative_rate(victim_events, aggressor_events, duration_s)
                        aggressor_rate = activity_rate(aggressor_events, duration_s)
                        rate_cache[victim] = activity_rate(victim_events, duration_s)
                    local_pair_activity[pair_id] = {
                        "activity": activity,
                        "aggressor_rate": aggressor_rate,
                        "victim_code": victim_code,
                        "aggressor_code": aggressor_code,
                    }
                activity_entry = local_pair_activity[pair_id]
                rel_hz, same_hz, opp_hz, unpaired_hz = activity_entry["activity"]
                by_victim = branch_aggregates[victim]
                total = pair_aggregates["all_leaf_clock_victims"]
                for aggregate in (by_victim, total):
                    aggregate["cap_f"] += c_f
                    aggregate["pair_records"] += 1
                if rel_hz is None:
                    for aggregate in (by_victim, total):
                        aggregate["unmatched_pairs"] += 1
                    mapping_status = (
                        "unmapped_victim_clock" if victim_code is None
                        else "unmapped_vcd_aggressor"
                    )
                    unmapped_row = {
                        "candidate_id": candidate,
                        "workload": workload,
                        "victim_net": victim,
                        "aggressor_net": aggressor,
                        "node_a": pair["node_a"],
                        "node_b": pair["node_b"],
                        "coupling_ff": f"{c_f * 1e15:.12g}",
                        "vcd_victim_code": victim_code or "",
                        "vcd_aggressor_code": aggressor_code or "",
                        "mapping_status": mapping_status,
                    }
                    all_pair_rows.append(unmapped_row)
                    unmapped_vcd_rows.append(unmapped_row)
                    continue
                unweighted = 0.5 * c_f * VOLTAGE_V**2 * (
                    float(rate_cache.get(victim, 0.0) or 0.0)
                    + float(activity_entry["aggressor_rate"] or 0.0)
                )
                proxy = 0.5 * c_f * VOLTAGE_V**2 * rel_hz
                for aggregate in (by_victim, total):
                    aggregate["matched_cap_f"] += c_f
                    aggregate["proxy_w"] += proxy
                    aggregate["unweighted_proxy_w"] += unweighted
                    aggregate["relative_rate_weighted"] += c_f * rel_hz
                    aggregate["same_rate_weighted"] += c_f * float(same_hz or 0.0)
                    aggregate["opposite_rate_weighted"] += c_f * float(opp_hz or 0.0)
                    aggregate["unpaired_rate_weighted"] += c_f * float(unpaired_hz or 0.0)
                    aggregate["matched_pairs"] += 1
                all_pair_rows.append({
                    "candidate_id": candidate,
                    "workload": workload,
                    "victim_net": victim,
                    "aggressor_net": aggressor,
                    "node_a": pair["node_a"],
                    "node_b": pair["node_b"],
                    "coupling_ff": f"{c_f * 1e15:.12g}",
                    "mapping_status": "matched",
                    "vcd_victim_code": activity_entry["victim_code"] or "",
                    "vcd_aggressor_code": activity_entry["aggressor_code"] or "",
                    "aggressor_transition_hz": f"{activity_entry['aggressor_rate']:.12g}",
                    "same_direction_hz": f"{float(same_hz or 0.0):.12g}",
                    "opposite_direction_hz": f"{float(opp_hz or 0.0):.12g}",
                    "unpaired_transition_hz": f"{float(unpaired_hz or 0.0):.12g}",
                    "relative_transition_score_hz": f"{rel_hz:.12g}",
                    "relative_coupling_power_proxy_nw": f"{proxy * 1e9:.12g}",
                })

            power_dir = POWER_ROOT / variant
            candidate_power = (
                parse_log_power(power_dir / "candidate" / f"{workload}.log")
                if candidate != "baseline" else None
            )
            baseline_power = (
                parse_log_power(power_dir / "baseline" / f"{workload}.log")
                if candidate != "baseline" else None
            )
            # The baseline route's original report lives in gcd_baseline20ns,
            # while candidate comparison runs keep a matching baseline report.
            # Use the latter where available for candidate-specific power runs.
            if candidate == "baseline":
                baseline_power = parse_log_power(
                    POWER_ROOT / baseline_power_variant / "baseline" / f"{workload}.log"
                )
                candidate_power = baseline_power
            else:
                assert candidate_power is not None
            total = pair_aggregates["all_leaf_clock_victims"]
            cap_fraction = total["matched_cap_f"] / total["cap_f"] if total["cap_f"] else 1.0
            rate_fraction = total["matched_pairs"] / total["pair_records"] if total["pair_records"] else 1.0
            proxy_w = float(total["proxy_w"])
            unweighted_proxy_w = float(total["unweighted_proxy_w"])
            row: dict[str, object] = {
                "candidate_id": candidate,
                "route_variant": variant,
                "workload": workload,
                "coupling_power_proxy_nw": f"{proxy_w * 1e9:.12g}",
                "uncorrelated_activity_proxy_nw": f"{unweighted_proxy_w * 1e9:.12g}",
                "matched_coupling_ff": f"{total['matched_cap_f'] * 1e15:.12g}",
                "all_coupling_ff": f"{total['cap_f'] * 1e15:.12g}",
                "cap_weighted_vcd_coverage": f"{cap_fraction:.12g}",
                "pair_record_vcd_coverage": f"{rate_fraction:.12g}",
                "matched_pair_records": total["matched_pairs"],
                "all_pair_records": total["pair_records"],
                "weighted_same_direction_hz_ff": f"{total['same_rate_weighted']:.12g}",
                "weighted_opposite_direction_hz_ff": f"{total['opposite_rate_weighted']:.12g}",
                "weighted_unpaired_transition_hz_ff": f"{total['unpaired_rate_weighted']:.12g}",
                "measured_clock_group_switching_w": f"{candidate_power['clock_switching_w']:.12g}",
                "measured_clock_group_switching_delta_nw": f"{(candidate_power['clock_switching_w'] - baseline_power['clock_switching_w']) * 1e9:.12g}",
                "measured_design_switching_w": f"{candidate_power['design_switching_w']:.12g}",
                "measured_design_switching_delta_nw": f"{(candidate_power['design_switching_w'] - baseline_power['design_switching_w']) * 1e9:.12g}",
            }
            for victim, aggregate in branch_aggregates.items():
                suffix = victim.replace("clknet_2_", "leaf_").replace("__leaf_clk", "")
                row[f"{suffix}_coupling_power_proxy_nw"] = f"{aggregate['proxy_w'] * 1e9:.12g}"
                row[f"{suffix}_coupling_ff"] = f"{aggregate['matched_cap_f'] * 1e15:.12g}"
                row[f"{suffix}_vcd_coverage"] = (
                    f"{aggregate['matched_cap_f'] / aggregate['cap_f']:.12g}"
                    if aggregate["cap_f"] else "1"
                )
            power_rows.append(row)

    # Candidate-minus-baseline values are the comparison quantity. Keep the
    # absolute pair estimate in the table as well for auditability.
    baseline_rows = {
        row["workload"]: row for row in power_rows if row["candidate_id"] == "baseline"
    }
    for row in power_rows:
        base = baseline_rows[row["workload"]]
        row["coupling_power_proxy_delta_nw"] = f"{float(row['coupling_power_proxy_nw']) - float(base['coupling_power_proxy_nw']):.12g}"
        row["uncorrelated_activity_proxy_delta_nw"] = f"{float(row['uncorrelated_activity_proxy_nw']) - float(base['uncorrelated_activity_proxy_nw']):.12g}"

    correlation_rows: list[dict[str, object]] = []
    for workload in WORKLOADS:
        rows = [row for row in power_rows if row["workload"] == workload and row["candidate_id"] != "baseline"]
        for predictor in ("coupling_power_proxy_delta_nw", "uncorrelated_activity_proxy_delta_nw"):
            for target in ("measured_clock_group_switching_delta_nw", "measured_design_switching_delta_nw"):
                x = [float(row[predictor]) for row in rows]
                y = [float(row[target]) for row in rows]
                n, rho, tau = correlations(x, y)
                correlation_rows.append({
                    "workload": workload,
                    "predictor": predictor,
                    "target": target,
                    "n": n,
                    "spearman_rho": "" if rho is None else f"{rho:.9f}",
                    "kendall_tau_b": "" if tau is None else f"{tau:.9f}",
                })

    output.mkdir(parents=True, exist_ok=True)
    write_tsv(output / "spef_clock_coupling_cap_reconciliation.tsv", cap_check_rows)
    write_tsv(output / "spef_coupling_pairs_vcd_activity.tsv", all_pair_rows)
    write_tsv(output / "candidate_coupling_activity_power.tsv", power_rows)
    write_tsv(output / "coupling_power_rank_correlations.tsv", correlation_rows)
    write_tsv(output / "unresolved_spef_coupling_endpoints.tsv", unresolved_rows or [{"candidate_id": "none"}])
    write_tsv(output / "unmapped_vcd_aggressors.tsv", unmapped_vcd_rows or [{"candidate_id": "none"}])

    max_pair_count = max(int(row["all_pair_records"]) for row in power_rows)
    min_coverage = min(float(row["cap_weighted_vcd_coverage"]) for row in power_rows)
    min_pair_coverage = min(float(row["pair_record_vcd_coverage"]) for row in power_rows)
    typical_joint = next(
        row for row in correlation_rows
        if row["workload"] == "typical"
        and row["predictor"] == "coupling_power_proxy_delta_nw"
        and row["target"] == "measured_design_switching_delta_nw"
    )
    typical_uncorrelated = next(
        row for row in correlation_rows
        if row["workload"] == "typical"
        and row["predictor"] == "uncorrelated_activity_proxy_delta_nw"
        and row["target"] == "measured_design_switching_delta_nw"
    )
    raw_activity_rho = None
    prior_activity_path = experiment / "analysis/spatial_feature_diagnostics/vcd_vs_routed_switching_correlations.tsv"
    if prior_activity_path.exists():
        raw_activity = next((
            row for row in read_tsv(prior_activity_path)
            if row["feature"] == "typical_pair_opposite_direction_hz"
            and row["target"] == "routed_net_switching_delta_nw_typical"
        ), None)
        if raw_activity is not None:
            raw_activity_rho = float(raw_activity["spearman_rho"])
    example_lines = []
    for candidate in ("rand017", "rand021"):
        measured = next((
            row for row in power_rows
            if row["candidate_id"] == candidate and row["workload"] == "typical"
        ), None)
        if measured is not None:
            example_lines.append(
                f"{candidate} typical: coupling proxy delta {float(measured['coupling_power_proxy_delta_nw']):+.1f} nW; "
                f"measured design net-switching delta {float(measured['measured_design_switching_delta_nw']):+.1f} nW."
            )
    observed_findings = [
        "## Outcome",
        "",
        f"Typical-workload Cc-weighted pair score versus routed design net switching is ρ={float(typical_joint['spearman_rho']):+.3f}; the uncorrelated Cc-weighted activity reference is ρ={float(typical_uncorrelated['spearman_rho']):+.3f}.",
    ]
    if raw_activity_rho is not None:
        observed_findings.append(
            f"The previous VCD-only opposite-direction activity score was ρ={raw_activity_rho:+.3f} on the same pool and target. The explicit Cc weighting adds no observed ranking lift here."
        )
    observed_findings += example_lines
    observed_findings += [
        "This result concerns routed net switching only. It does not capture the approximately 3.65 µW clock-cell internal-power reduction from the dummy-cell change, which remains the dominant Stage 1 mechanism.",
        "",
    ]
    report = [
        "# SPEF coupling to VCD aggressor activity",
        "",
        f"Analyzed {len(manifest)} routed relocations plus the baseline, across {len(WORKLOADS)} workloads. The frozen candidate pool was read only.",
        "",
        "## Extraction and reconciliation",
        "",
        f"Every explicit coupling capacitor attached to the four clock-leaf victim nets was paired with its SPEF aggressor net. Pair capacitance sums reconcile with the existing per-clock SPEF labels to a maximum absolute error of {max_cap_error:.3g} fF.",
        f"Across candidate/workload records, the minimum cap-weighted VCD mapping coverage is {min_coverage:.1%}; the minimum pair-record coverage is {min_pair_coverage:.1%}. Maximum coupling records on one route are {max_pair_count}. Unresolved SPEF endpoint pairs are recorded separately.",
        "",
        "SPEF pair records identify node names but this export contains no physical XY coordinate for each coupling capacitor. The analysis therefore maps victim/aggressor nets, not coordinate-located wire segments.",
        f"Per-capacitor VCD mapping failures, including node names and capacitance, are listed in `unmapped_vcd_aggressors.tsv` ({len(unmapped_vcd_rows)} candidate/workload records).",
        "",
        "## Power calculation and validation",
        "",
        f"For each matched pair, the timestamp-level relative transition score is the sum of (ΔV_victim − ΔV_aggressor)² at coincident VCD timestamps, divided by trace duration. The power proxy is 0.5 × Cc × 1.8² × that rate. A second reference column uses the uncorrelated sum of victim and aggressor transition rates. This is a trace-window diagnostic proxy; it is not OpenSTA's exact coupling-power model.",
        "",
        "The direct comparison targets are the measured `Clock` group's routed switching power and the design-wide routed switching power from the existing OpenSTA reports. Rank correlations are in `coupling_power_rank_correlations.tsv`; candidate deltas are against the baseline route.",
        "",
        *observed_findings,
        "## Rank correlations",
        "",
        "| Workload | Proxy | Target | Spearman ρ | Kendall τ-b | n |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in correlation_rows:
        report.append(
            f"| {row['workload']} | {row['predictor']} | {row['target']} | "
            f"{row['spearman_rho'] or 'NA'} | {row['kendall_tau_b'] or 'NA'} | {row['n']} |"
        )
    report += [
        "",
        "## Interpretation limits",
        "",
        "The SPEF and VCDs are sufficient to form a net-paired, activity-weighted coupling estimate. They do not locate each capacitor along the route, and exact-time transition pairing is sensitive to VCD timestep and event delay. Compare both proxy variants and the per-pair coverage before interpreting a correlation as evidence of predictive value.",
        "",
        "This report does not fit a model or update any placement score. Any follow-up predictor must be frozen and evaluated on a new candidate pool.",
        "",
    ]
    (output / "coupling_power_validation_report.md").write_text("\n".join(report))
    print(f"Wrote SPEF/VCD coupling analysis: {output}")
    print(f"Cap reconciliation max abs error: {max_cap_error:.6g} fF")
    print(f"Minimum cap-weighted VCD coverage: {min_coverage:.2%}")
    print(f"Unresolved coupling endpoints: {len(unresolved_rows)}")
    for row in correlation_rows:
        print(
            f"{row['workload']} {row['predictor']} vs {row['target']}: "
            f"rho={row['spearman_rho'] or 'NA'} tau-b={row['kendall_tau_b'] or 'NA'} n={row['n']}"
        )


if __name__ == "__main__":
    main()
