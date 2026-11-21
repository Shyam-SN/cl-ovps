#!/usr/bin/env python3
"""Freeze branch-FLUTE and tier-gate predictions before routing a GCD holdout."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import subprocess
from pathlib import Path

from analyze_gcd_cts_branch_proxy import kendall_tau_b, spearman


ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
VOLTAGE = 1.8
CUNIT_FF_PER_UM = 0.214962
CLOCK_TRANSITION_DENSITY_HZ = 100_000_000.0
WORKLOADS = ("idle", "typical", "peak")


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dump_tcl_list(values: list[int]) -> str:
    return "{" + " ".join(str(value) for value in values) + "}"


def export_flute_lengths(oracle_manifest: Path, inventory_dir: Path, analysis_dir: Path) -> tuple[dict[tuple[str, str], int], list[dict[str, object]]]:
    manifest = read_tsv(oracle_manifest)
    cases: list[tuple[str, str, list[tuple[int, int]]]] = []
    for row in manifest:
        candidate = row["candidate_id"]
        path = inventory_dir / f"{candidate}_sinks.tsv"
        sink_rows = read_tsv(path)
        grouped: dict[str, list[tuple[int, int]]] = {}
        for sink in sink_rows:
            branch = sink["branch"]
            if "__leaf_clk" not in branch:
                continue
            grouped.setdefault(branch, []).append((int(sink["x_dbu"]), int(sink["y_dbu"])))
        for branch, raw_points in sorted(grouped.items()):
            points = sorted(set(raw_points))
            cases.append((candidate, branch, points))

    tcl_lines: list[str] = []
    direct_lengths: dict[tuple[str, str], int] = {}
    for candidate, branch, points in cases:
        if len(points) <= 1:
            direct_lengths[(candidate, branch)] = 0
            continue
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        tcl_lines.append(f'puts "FLUTE_CASE {candidate} {branch}"')
        tcl_lines.append(f"stt::report_flute_tree {dump_tcl_list(xs)} {dump_tcl_list(ys)} 0")
    tcl_path = analysis_dir / "flute_oracle.tcl"
    log_path = analysis_dir / "flute_oracle.log"
    tcl_path.write_text("\n".join(tcl_lines) + "\n")
    process = subprocess.run(
        [str(OPENROAD), "-no_init", "-exit", str(tcl_path)],
        cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"OpenROAD FLUTE export failed; see {log_path}")
    lengths = dict(direct_lengths)
    current: tuple[str, str] | None = None
    for line in process.stdout.splitlines():
        marker = re.search(r"FLUTE_CASE (\S+) (\S+)", line)
        if marker:
            current = (marker.group(1), marker.group(2))
        wire = re.search(r"Wire length = (\d+)", line)
        if wire and current:
            lengths[current] = int(wire.group(1))
    expected = {(candidate, branch) for candidate, branch, _ in cases}
    missing = expected - lengths.keys()
    if missing:
        raise ValueError(f"FLUTE did not report {len(missing)} candidate/branch lengths: {sorted(missing)[:5]}")
    rows = []
    count_map = {(candidate, branch): len(points) for candidate, branch, points in cases}
    for candidate, branch in sorted(lengths):
        length = lengths[(candidate, branch)]
        rows.append({
            "candidate_id": candidate,
            "branch": branch,
            "sink_count": count_map[(candidate, branch)],
            "flute_length_dbu": length,
            "flute_length_um": length / 1000.0,
        })
    return lengths, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--oracle-manifest", type=Path, required=True)
    parser.add_argument("--oracle-score-dir", type=Path, required=True)
    parser.add_argument("--oracle-inventory-dir", type=Path, required=True)
    parser.add_argument("--cunit-ff-per-um", type=float, default=CUNIT_FF_PER_UM)
    parser.add_argument("--clock-transition-density-hz", type=float, default=CLOCK_TRANSITION_DENSITY_HZ)
    parser.add_argument("--tier1-internal-cutoff-nw", type=float, default=-1000.0)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    oracle_manifest = args.oracle_manifest.resolve()
    score_dir = args.oracle_score_dir.resolve()
    inventory_dir = args.oracle_inventory_dir.resolve()
    score_rows = read_tsv(score_dir / "frozen_oracle_scores.tsv")
    score_by = {(row["candidate_id"], row["workload"]): row for row in score_rows}
    audit_rows = read_tsv(experiment / "relocation_audit.tsv")
    audit_by = {row["candidate_id"]: row for row in audit_rows}
    pool_rows = read_tsv(experiment / "candidate_pool.tsv")
    pool_by = {row["candidate_id"]: row for row in pool_rows}
    manifest_rows = read_tsv(oracle_manifest)
    oracle_ids = {row["candidate_id"] for row in manifest_rows}
    if oracle_ids != {"baseline", *pool_by.keys()}:
        raise SystemExit("CTS oracle candidate IDs do not match the frozen pool")
    cell_maps = {}
    sink_maps = {}
    for candidate in oracle_ids:
        cells = read_tsv(inventory_dir / f"{candidate}_cells.tsv")
        sinks = read_tsv(inventory_dir / f"{candidate}_sinks.tsv")
        cell_maps[candidate] = {
            row["instance"]: row["master"]
            for row in cells if row["instance"].startswith("clkload")
        }
        sink_maps[candidate] = {row["instance"]: row["branch"] for row in sinks}
        if len(sinks) != 35:
            raise ValueError(f"{candidate} has {len(sinks)} CTS sinks; expected 35")
    baseline_branches = sorted(set(sink_maps["baseline"].values()))
    if len(baseline_branches) != 4:
        raise ValueError(f"baseline CTS has {len(baseline_branches)} leaf branches")

    lengths, flute_rows = export_flute_lengths(oracle_manifest, inventory_dir, analysis)
    write_tsv(analysis / "frozen_flute_branch_features.tsv", flute_rows)
    flute_totals = {}
    for candidate in oracle_ids:
        branches = {branch for cid, branch in lengths if cid == candidate}
        flute_totals[candidate] = sum(lengths[(candidate, branch)] for branch in branches) / 1000.0
    base_branch_lengths = {
        branch: lengths.get(("baseline", branch), 0) / 1000.0
        for branch in baseline_branches
    }
    stage2_rows: list[dict[str, object]] = []
    for candidate_id, proposal in sorted(pool_by.items()):
        audit = audit_by[candidate_id]
        branch_lengths = {branch: lengths.get((candidate_id, branch), 0) / 1000.0 for branch in baseline_branches}
        # Include newly named leaf nets if the CTS oracle changes net naming.
        branch_lengths.update({
            branch: lengths[(candidate_id, branch)] / 1000.0
            for cid, branch in lengths if cid == candidate_id and branch not in branch_lengths
        })
        delta_wl_um = sum(branch_lengths.values()) - sum(base_branch_lengths.values())
        delta_cap_ff = delta_wl_um * args.cunit_ff_per_um
        flute_power_nw = (
            0.5 * VOLTAGE**2 * delta_cap_ff * 1e-15
            * args.clock_transition_density_hz * 1e9
        )
        changed_dummy = sorted(
            name for name in set(cell_maps["baseline"]) | set(cell_maps[candidate_id])
            if cell_maps["baseline"].get(name) != cell_maps[candidate_id].get(name)
        )
        branch_changes = sum(
            sink_maps["baseline"].get(instance) != sink_maps[candidate_id].get(instance)
            for instance in set(sink_maps["baseline"]) | set(sink_maps[candidate_id])
        )
        result: dict[str, object] = {
            "candidate_id": candidate_id,
            "instance": proposal["instance"],
            "branch_before": proposal.get("branch_before", ""),
            "move_type": proposal.get("move_type", ""),
            "from_x_dbu": proposal["from_x_dbu"],
            "from_y_dbu": proposal["from_y_dbu"],
            "from_orient": proposal.get("from_orient", ""),
            "target_x_dbu": proposal["target_x_dbu"],
            "target_y_dbu": proposal["target_y_dbu"],
            "target_orient": proposal.get("target_orient", ""),
            "signal_hpwl_delta_pct": float(audit["signal_hpwl_delta_pct"]),
            "placement_timing_status": audit["status"],
            "oracle_sink_branch_changes": branch_changes,
            "flute_leaf_delta_wl_um": delta_wl_um,
            "flute_leaf_delta_cap_ff": delta_cap_ff,
            "flute_clock_wire_delta_nw": flute_power_nw,
            "dummy_master_changes": ";".join(
                f"{name}:{cell_maps['baseline'].get(name, 'ABSENT')}->{cell_maps[candidate_id].get(name, 'ABSENT')}"
                for name in changed_dummy
            ),
            "dummy_master_change_count": len(changed_dummy),
        }
        internal_values = {}
        oracle_dynamic_values = {}
        for workload in WORKLOADS:
            oracle = score_by[(candidate_id, workload)]
            internal = float(oracle["oracle_internal_delta_nw"])
            cell_dynamic = float(oracle["oracle_clock_dynamic_delta_nw"])
            place = float(audit[f"{workload}_delta_w"]) * 1e9
            combined_net = place + flute_power_nw
            result[f"placement_{workload}_switching_delta_nw"] = place
            result[f"combined_{workload}_net_switching_score_nw"] = combined_net
            result[f"oracle_{workload}_cts_internal_delta_nw"] = internal
            result[f"oracle_{workload}_cts_dynamic_delta_nw"] = cell_dynamic
            result[f"combined_{workload}_dynamic_score_nw"] = combined_net + cell_dynamic
            internal_values[workload] = internal
            oracle_dynamic_values[workload] = cell_dynamic
        tier1_event = bool(changed_dummy) and internal_values["typical"] <= args.tier1_internal_cutoff_nw
        tier1_pass = tier1_event and float(audit["signal_hpwl_delta_pct"]) <= 1.5
        tier2_pass = (
            not changed_dummy
            and float(audit["signal_hpwl_delta_pct"]) <= 0.5
            and all(float(result[f"combined_{w}_net_switching_score_nw"]) < 0.0 for w in WORKLOADS)
        )
        result["tier1_favorable_dummy_event"] = int(tier1_event)
        result["tier1_gate_pass"] = int(tier1_pass)
        result["tier2_gate_pass"] = int(tier2_pass)
        result["frozen_gate_decision"] = "TIER1_PASS" if tier1_pass else "TIER2_PASS" if tier2_pass else "REJECT"
        stage2_rows.append(result)

    flute_path = analysis / "frozen_flute_branch_features.tsv"
    write_tsv(flute_path, flute_rows)
    prediction_path = analysis / "frozen_tiered_predictions.tsv"
    write_tsv(prediction_path, stage2_rows)

    hashes = []
    source_paths = [
        experiment / "candidate_pool.tsv",
        experiment / "relocation_audit.tsv",
        experiment / "frozen_pool_integrity.tsv",
        experiment / "move_overlap_audit.tsv",
        experiment / "previous_pool_source_manifest.tsv",
        oracle_manifest,
        score_dir / "frozen_oracle_scores.tsv",
        score_dir / "frozen_score_integrity.tsv",
        inventory_dir / "oracle_inventory_manifest.tsv",
        analysis / "frozen_flute_branch_features.tsv",
        analysis / "frozen_tiered_predictions.tsv",
        analysis / "flute_oracle.tcl",
        ROOT / "scripts/score_gcd_cross_row_holdout.py",
        ROOT / "scripts/export_gcd_cts_oracle_pool.py",
        ROOT / "scripts/export_gcd_cts_oracle_data.tcl",
        OPENROAD,
    ]
    source_paths.extend(sorted((experiment / "candidate_odb").glob("*.odb")))
    source_paths.extend(Path(row["cts_odb"]) for row in manifest_rows)
    for path in source_paths:
        if path.is_file():
            hashes.append({"path": str(path.resolve()), "sha256": sha256(path), "size_bytes": path.stat().st_size})
    integrity_path = analysis / "pre_route_score_integrity.tsv"
    write_tsv(integrity_path, hashes)
    with integrity_path.open("a") as output:
        output.write(f"score_parameters\tCUNIT_FF_PER_UM={args.cunit_ff_per_um};CLOCK_TRANSITION_DENSITY_HZ={args.clock_transition_density_hz};TIER1_INTERNAL_CUTOFF_NW={args.tier1_internal_cutoff_nw}\tFROZEN_BEFORE_ROUTE\n")

    print(
        f"GCD_CROSS_ROW_SCORE candidates={len(stage2_rows)} favorable_dummy={sum(row['tier1_favorable_dummy_event'] for row in stage2_rows)} "
        f"tier1_pass={sum(row['tier1_gate_pass'] for row in stage2_rows)} tier2_pass={sum(row['tier2_gate_pass'] for row in stage2_rows)} "
        f"output={prediction_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
