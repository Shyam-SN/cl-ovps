#!/usr/bin/env python3
"""Extract fixed-VCD dummy-cell internal-power deltas from frozen CTS reports."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


WORKLOADS = ("idle", "typical", "peak")
ROW_RE = re.compile(
    r"^\s*([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+"
    r"([0-9.eE+-]+)\s+(clkload[^\s]*)$",
    re.MULTILINE,
)


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty TSV: {path}")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def dummy_internal_w(log_path: Path) -> dict[str, float]:
    result: dict[str, float] = {}
    for match in ROW_RE.finditer(log_path.read_text()):
        result[match.group(5)] = float(match.group(1))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--score-dir", type=Path, required=True)
    parser.add_argument("--oracle-inventory-dir", type=Path, required=True)
    parser.add_argument("--internal-cutoff-nw", type=float, default=-1000.0)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    score_dir = args.score_dir.resolve()
    inventory_dir = args.oracle_inventory_dir.resolve()
    pool = read_tsv(experiment / "candidate_pool.tsv")
    audit = {row["candidate_id"]: row for row in read_tsv(experiment / "relocation_audit.tsv")}

    master_maps: dict[str, dict[str, str]] = {}
    for path in inventory_dir.glob("*_cells.tsv"):
        candidate_id = path.stem.removesuffix("_cells")
        master_maps[candidate_id] = {
            row["instance"]: row["master"]
            for row in read_tsv(path) if row["instance"].startswith("clkload")
        }
    if "baseline" not in master_maps or any(row["candidate_id"] not in master_maps for row in pool):
        raise ValueError("CTS dummy-cell inventory does not cover baseline and every frozen candidate")

    baseline_power = {
        workload: dummy_internal_w(score_dir / "power_logs" / "baseline" / f"{workload}.log")
        for workload in WORKLOADS
    }
    score_rows: list[dict[str, object]] = []
    summary_rows: list[dict[str, object]] = []
    base_masters = master_maps["baseline"]
    for proposal in pool:
        candidate = proposal["candidate_id"]
        candidate_masters = master_maps[candidate]
        changed = sorted(
            name for name in base_masters.keys() | candidate_masters.keys()
            if base_masters.get(name) != candidate_masters.get(name)
        )
        deltas: dict[str, float] = {}
        for workload in WORKLOADS:
            candidate_power = dummy_internal_w(score_dir / "power_logs" / candidate / f"{workload}.log")
            baseline_sum = sum(baseline_power[workload].values())
            candidate_sum = sum(candidate_power.values())
            delta_nw = (candidate_sum - baseline_sum) * 1e9
            deltas[workload] = delta_nw
            score_rows.append({
                "candidate_id": candidate,
                "workload": workload,
                "baseline_dummy_internal_nw": baseline_sum * 1e9,
                "candidate_dummy_internal_nw": candidate_sum * 1e9,
                "dummy_internal_delta_nw": delta_nw,
                "dummy_master_changes": ";".join(
                    f"{name}:{base_masters.get(name, 'ABSENT')}->{candidate_masters.get(name, 'ABSENT')}"
                    for name in changed
                ),
                "dummy_master_change_count": len(changed),
                "fixed_vcd_source": "baseline_workload_trace",
                "parasitics": "placement_estimate_after_CTS",
            })
        hpwl_pct = float(audit[candidate]["signal_hpwl_delta_pct"])
        tier1_pass = bool(changed) and deltas["typical"] <= args.internal_cutoff_nw and hpwl_pct <= 1.5
        summary_rows.append({
            "candidate_id": candidate,
            "instance": proposal["instance"],
            "branch_before": proposal.get("branch_before", ""),
            "move_type": proposal.get("move_type", ""),
            "signal_hpwl_delta_pct": hpwl_pct,
            "dummy_master_change_count": len(changed),
            "dummy_master_changes": ";".join(
                f"{name}:{base_masters.get(name, 'ABSENT')}->{candidate_masters.get(name, 'ABSENT')}"
                for name in changed
            ),
            **{f"{w}_dummy_internal_delta_nw": deltas[w] for w in WORKLOADS},
            "tier1_gate_pass": int(tier1_pass),
            "frozen_gate_decision": "TIER1_PASS" if tier1_pass else "NO_TIER1_EVENT",
        })

    output_dir = experiment / "analysis"
    write_tsv(output_dir / "frozen_tier1_dummy_scores.tsv", score_rows)
    write_tsv(output_dir / "frozen_tier1_gate_predictions.tsv", summary_rows)
    print(f"FROZEN_TIER1 candidates={len(summary_rows)} gate_pass={sum(int(row['tier1_gate_pass']) for row in summary_rows)} "
          f"output={output_dir / 'frozen_tier1_gate_predictions.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
