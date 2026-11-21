#!/usr/bin/env python3
"""Compare CTS-only placement-oracle output with routed CTS and power."""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
from pathlib import Path

from analyze_gcd_cts_branch_proxy import (
    cts_cell_masters,
    dummy_loads_by_branch,
    inventory,
    kendall_tau_b,
    read_tsv,
    spearman,
    write_tsv,
)


ROOT = Path(__file__).resolve().parents[1]


def run_export(openroad: Path, tcl: Path, odb: Path, sink_out: Path, cell_out: Path) -> None:
    env = os.environ.copy()
    env.update({
        "CTS_ORACLE_ODB": str(odb.resolve()),
        "CTS_ORACLE_SINK_OUT": str(sink_out.resolve()),
        "CTS_ORACLE_CELL_OUT": str(cell_out.resolve()),
    })
    process = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(tcl)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if process.returncode or not sink_out.is_file() or not cell_out.is_file():
        raise RuntimeError(
            f"CTS oracle export failed for {odb}:\n{process.stdout}"
        )


def export_inventory(path: Path) -> dict[str, dict[str, object]]:
    rows = read_tsv(path)
    return {
        row["instance"]: {
            "branch": row["branch"],
            "cap": float(row["cap_ff"]),
        }
        for row in rows
    }


def export_cells(path: Path) -> dict[str, dict[str, object]]:
    return {
        row["instance"]: {
            "master": row["master"],
            "input_net": row["input_net"],
            "input_cap": float(row["input_cap_ff"]) if row["input_cap_ff"] else 0.0,
        }
        for row in read_tsv(path)
    }


def run_power_report(
    openroad: Path,
    tcl: Path,
    odb: Path,
    sdc: Path,
    vcd: Path,
    workload: str,
    log: Path,
) -> tuple[float, float, float, float]:
    env = os.environ.copy()
    env.update({
        "CTS_ORACLE_ODB": str(odb.resolve()),
        "CTS_ORACLE_SDC": str(sdc.resolve()),
        "ACTIVITY_VCD": str(vcd.resolve()),
        "WORKLOAD": workload,
    })
    process = subprocess.run(
        [str(openroad), "-no_init", "-exit", str(tcl)],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"CTS placement-power report failed: {log}")
    pattern = re.compile(
        r"^\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+"
        r"([0-9.eE+-]+)\s+(clk(?:buf|load)[^\s]*)$",
        re.MULTILINE,
    )
    values = [tuple(float(match.group(i)) for i in range(1, 5)) for match in pattern.finditer(process.stdout)]
    if not values:
        raise RuntimeError(f"no CTS power rows parsed from {log}")
    return tuple(sum(values[j][i] for j in range(len(values))) for i in range(4))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-dir", type=Path, default=Path("build/power/gcd_flop_relocation_random_local_20261029b"))
    parser.add_argument("--oracle-manifest", type=Path, default=Path("build/power/gcd_flop_relocation_random_local_20261029b/analysis/cts_oracle_manifest.tsv"))
    parser.add_argument("--route-manifest", type=Path, default=Path("build/power/gcd_flop_relocation_random_local_20261029b/route_manifest.tsv"))
    parser.add_argument("--results-root", type=Path, default=Path("build/orfs/results/sky130hd/gcd_power"))
    parser.add_argument("--activity-root", type=Path, default=Path("build/activity"))
    parser.add_argument("--output-dir", type=Path, default=Path("build/power/gcd_flop_relocation_random_local_20261029b/analysis/cts_oracle"))
    parser.add_argument("--vcd-mode", choices=("candidate", "baseline"), default="candidate")
    args = parser.parse_args()

    def rooted(path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (ROOT / path).resolve()

    pool_dir = rooted(args.pool_dir)
    results_root = rooted(args.results_root)
    activity_root = rooted(args.activity_root)
    output_dir = rooted(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    oracle_manifest = read_tsv(rooted(args.oracle_manifest))
    route_manifest = {row["candidate_id"]: row for row in read_tsv(rooted(args.route_manifest))}
    metrics = {row["candidate_id"]: row for row in read_tsv(pool_dir / "analysis/candidate_metrics.tsv")}
    inventory_dir = pool_dir / "analysis/branch_proxy/inventories"
    openroad = ROOT / "tools/openroad/build/bin/openroad"
    export_tcl = ROOT / "scripts/export_gcd_cts_oracle_data.tcl"
    power_tcl = ROOT / "scripts/gcd_report_placed_cts_power.tcl"
    oracle_data_dir = output_dir / "inventories"
    oracle_data_dir.mkdir(parents=True, exist_ok=True)

    comparison_rows: list[dict[str, object]] = []
    dummy_rows: list[dict[str, object]] = []
    oracle_power: dict[str, dict[str, tuple[float, float, float, float]]] = {}
    cts_runtime_total = 0.0
    workloads = ("idle", "typical", "peak")

    for row in oracle_manifest:
        candidate_id = row["candidate_id"]
        if candidate_id == "baseline":
            route_result_dir = results_root / "gcd_baseline20ns"
            route_sinks_path = inventory_dir / "baseline_cts.tsv"
            route_variant = "gcd_baseline20ns"
        else:
            if candidate_id not in route_manifest:
                raise KeyError(f"no routed candidate found for {candidate_id}")
            route_variant = route_manifest[candidate_id]["route_variant"]
            route_result_dir = results_root / route_variant
            route_sinks_path = inventory_dir / f"{candidate_id}_cts.tsv"

        oracle_result_dir = Path(row["result_dir"])
        oracle_odb = Path(row["cts_odb"])
        oracle_sdc = Path(row["cts_sdc"])
        sink_out = oracle_data_dir / f"{candidate_id}_sinks.tsv"
        cell_out = oracle_data_dir / f"{candidate_id}_cells.tsv"
        run_export(openroad, export_tcl, oracle_odb, sink_out, cell_out)
        oracle_sinks = export_inventory(sink_out)
        oracle_cells = export_cells(cell_out)
        route_sinks = inventory(route_sinks_path)
        route_sink_branches = {
            instance: str(item["branch"]) for instance, item in route_sinks.items()
        }
        oracle_sink_branches = {
            instance: str(item["branch"]) for instance, item in oracle_sinks.items()
        }
        common_sinks = route_sink_branches.keys() & oracle_sink_branches.keys()
        sink_mismatches = sum(
            route_sink_branches[instance] != oracle_sink_branches[instance]
            for instance in common_sinks
        ) + len(route_sink_branches.keys() ^ oracle_sink_branches.keys())

        oracle_clock_cells = {
            name: (str(cell["master"]), str(cell["input_net"]))
            for name, cell in oracle_cells.items()
        }
        route_clock_masters = cts_cell_masters(route_result_dir / "6_final.v")
        route_dummy_nets = dummy_loads_by_branch(route_result_dir / "6_final.v")
        route_clock_cells = {
            name: (master, "") for name, master in route_clock_masters.items()
        }
        route_clock_cells.update({
            instance: (master, branch)
            for branch, (instance, master) in route_dummy_nets.items()
        })
        oracle_dummy_nets = {
            name: (str(cell["master"]), str(cell["input_net"]))
            for name, cell in oracle_cells.items()
            if name.startswith("clkload")
        }
        route_dummy_cell_nets = {
            instance: (master, branch)
            for branch, (instance, master) in route_dummy_nets.items()
        }
        oracle_master_map = {name: value[0] for name, value in oracle_clock_cells.items()}
        route_master_map = {
            name: value[0] for name, value in route_clock_cells.items()
        }
        master_mismatches = sum(
            oracle_master_map.get(name) != route_master_map.get(name)
            for name in oracle_master_map.keys() | route_master_map.keys()
        )
        dummy_net_mismatches = sum(
            oracle_dummy_nets.get(name) != route_dummy_cell_nets.get(name)
            for name in oracle_dummy_nets.keys() | route_dummy_cell_nets.keys()
        )

        branch_loads: dict[str, float] = {}
        branch_counts: dict[str, int] = {}
        for item in oracle_sinks.values():
            branch = str(item["branch"])
            branch_loads[branch] = branch_loads.get(branch, 0.0) + float(item["cap"])
            branch_counts[branch] = branch_counts.get(branch, 0) + 1
        max_load = max(branch_loads.values())
        dummy_by_net = {
            str(cell["input_net"]): (name, str(cell["master"]), float(cell["input_cap"]))
            for name, cell in oracle_cells.items()
            if name.startswith("clkload")
        }
        for branch, load in sorted(branch_loads.items()):
            deficit = max_load - load
            dummy = dummy_by_net.get(branch)
            dummy_rows.append({
                "candidate_id": candidate_id,
                "branch": branch,
                "sink_count": branch_counts[branch],
                "child_output_cap_ff": load,
                "max_sibling_output_cap_ff": max_load,
                "dummy_cap_deficit_ff": deficit,
                "dummy_instance": dummy[0] if dummy else "",
                "dummy_master": dummy[1] if dummy else "",
                "dummy_input_cap_ff": dummy[2] if dummy else "",
                "dummy_cap_match_error_ff": abs(dummy[2] - deficit) if dummy else "",
            })

        comparison_rows.append({
            "candidate_id": candidate_id,
            "cts_wall_seconds": row["elapsed_seconds"],
            "sink_count": len(oracle_sinks),
            "sink_branch_mismatch_count_vs_routed_cts": sink_mismatches,
            "cts_master_mismatch_count_vs_routed": master_mismatches,
            "dummy_net_mismatch_count_vs_routed": dummy_net_mismatches,
            "oracle_dummy_master_changes": ";".join(
                f"{name}:{master}" for name, (master, _) in sorted(oracle_dummy_nets.items())
            ),
            "routed_dummy_masters": ";".join(
                f"{name}:{master}" for name, (master, _) in sorted(route_dummy_cell_nets.items())
            ),
        })
        cts_runtime_total += float(row["elapsed_seconds"])

        oracle_power[candidate_id] = {}
        for workload in workloads:
            if args.vcd_mode == "baseline" or candidate_id == "baseline":
                vcd = activity_root / "gcd_baseline20ns/routed" / f"{workload}.vcd"
            else:
                vcd = activity_root / route_variant / "candidate" / f"{workload}.vcd"
            log = output_dir / "power_logs" / candidate_id / f"{workload}.log"
            oracle_power[candidate_id][workload] = run_power_report(
                openroad, power_tcl, oracle_odb, oracle_sdc, vcd, workload, log
            )

    baseline_power = oracle_power["baseline"]
    power_rows: list[dict[str, object]] = []
    correlations: list[dict[str, object]] = []
    for workload in workloads:
        targets = {
            "routed_clock_internal_delta_nw": [],
            "routed_clock_switching_delta_nw": [],
            "routed_clock_dynamic_delta_nw": [],
        }
        predictors = {
            "oracle_cts_internal_delta_nw": [],
            "oracle_cts_switching_delta_nw": [],
            "oracle_cts_internal_plus_switching_delta_nw": [],
        }
        candidate_ids = sorted(metrics)
        for candidate_id in candidate_ids:
            power = oracle_power[candidate_id][workload]
            base = baseline_power[workload]
            internal_delta = (power[0] - base[0]) * 1e9
            switching_delta = (power[1] - base[1]) * 1e9
            dynamic_delta = internal_delta + switching_delta
            power_rows.append({
                "candidate_id": candidate_id,
                "workload": workload,
                "oracle_cts_internal_nw": power[0] * 1e9,
                "oracle_cts_switching_nw": power[1] * 1e9,
                "oracle_cts_leakage_nw": power[2] * 1e9,
                "oracle_cts_dynamic_nw": (power[0] + power[1]) * 1e9,
                "oracle_cts_internal_delta_nw": internal_delta,
                "oracle_cts_switching_delta_nw": switching_delta,
                "oracle_cts_internal_plus_switching_delta_nw": dynamic_delta,
                "routed_clock_internal_delta_nw": float(metrics[candidate_id][f"routed_{workload}_clock_internal_delta_nw"]),
                "routed_clock_switching_delta_nw": float(metrics[candidate_id][f"routed_{workload}_clock_switching_delta_nw"]),
                "routed_clock_dynamic_delta_nw": float(metrics[candidate_id][f"routed_{workload}_clock_total_delta_nw"]),
            })
            if candidate_id == "baseline":
                continue
            predictors["oracle_cts_internal_delta_nw"].append(internal_delta)
            predictors["oracle_cts_switching_delta_nw"].append(switching_delta)
            predictors["oracle_cts_internal_plus_switching_delta_nw"].append(dynamic_delta)
            targets["routed_clock_internal_delta_nw"].append(float(metrics[candidate_id][f"routed_{workload}_clock_internal_delta_nw"]))
            targets["routed_clock_switching_delta_nw"].append(float(metrics[candidate_id][f"routed_{workload}_clock_switching_delta_nw"]))
            targets["routed_clock_dynamic_delta_nw"].append(float(metrics[candidate_id][f"routed_{workload}_clock_total_delta_nw"]))
        for predictor, predicted_values in predictors.items():
            for target, actual_values in targets.items():
                correlations.append({
                    "workload": workload,
                    "predictor": predictor,
                    "target": target,
                    "candidate_count": len(predicted_values),
                    "spearman_rho": spearman(predicted_values, actual_values),
                    "kendall_tau_b": kendall_tau_b(predicted_values, actual_values),
                })

    write_tsv(output_dir / "oracle_vs_routed_cts.tsv", comparison_rows)
    write_tsv(output_dir / "oracle_sibling_dummy_deficits.tsv", dummy_rows)
    write_tsv(output_dir / "oracle_cts_power.tsv", power_rows)
    write_tsv(output_dir / "oracle_power_rank_correlations.tsv", correlations)

    exact_sink = sum(int(row["sink_branch_mismatch_count_vs_routed_cts"]) == 0 for row in comparison_rows)
    exact_master = sum(int(row["cts_master_mismatch_count_vs_routed"]) == 0 for row in comparison_rows)
    exact_dummy_net = sum(int(row["dummy_net_mismatch_count_vs_routed"]) == 0 for row in comparison_rows)
    summary = [
        "# CTS-only placement oracle validation",
        "",
        f"Ran CTS-only for {len(oracle_manifest)} placements; total flow wall time recorded by the runner was {cts_runtime_total:.1f} s.",
        f"Exact register sink-to-leaf assignment versus routed CTS: {exact_sink}/{len(comparison_rows)}.",
        f"Exact clock-cell master set versus routed netlist: {exact_master}/{len(comparison_rows)}.",
        f"Exact dummy-cell branch/net assignment versus routed netlist: {exact_dummy_net}/{len(comparison_rows)}.",
        f"The prediction uses CTS output and placement parasitics with `{args.vcd_mode}` VCD activity. It bypasses global and detailed routing. Candidate VCD mode requires a trace from the completed candidate route; baseline mode uses the same workload trace for every candidate, which is a pre-route-compatible activity assumption if the trace is prepared before placement.",
        "",
        "## Placement-CTS power rank correlations",
        "",
    ]
    for row in correlations:
        if row["target"] == "routed_clock_dynamic_delta_nw":
            summary.append(
                f"- {row['workload']}: `{row['predictor']}` vs routed clock dynamic power: "
                f"Spearman ρ={float(row['spearman_rho']):.3f}, Kendall τb={float(row['kendall_tau_b']):.3f}."
            )
    summary.extend([
        "",
        "Treat this comparison as a holdout only when its score file was frozen before routing and the candidate set was not used to tune the score. Keep the frozen score and its input hashes with the routed comparison.",
        "",
        "See `oracle_vs_routed_cts.tsv`, `oracle_sibling_dummy_deficits.tsv`, `oracle_cts_power.tsv`, and `oracle_power_rank_correlations.tsv` for exact topology comparison, branch deficits, estimated cell power, and rank metrics.",
    ])
    (output_dir / "summary.md").write_text("\n".join(summary) + "\n")
    print(f"Analyzed {len(oracle_manifest)} CTS oracle outputs; results: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
