#!/usr/bin/env python3
"""Rescore a frozen AES CTS trial set using total clock-cell internal power.

This is a diagnostic scorer only. It preserves and verifies the frozen pool,
then reads already-produced `report_power -instances` logs for each CTS trial.
It does not treat a partial RTL VCD as a signoff-quality AES power trace.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from pathlib import Path


POWER_ROW = re.compile(
    r"^\s*([0-9.eE+-]+)\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+[0-9.eE+-]+\s+((?:clkload|clkbuf|clkinv)[^\s]*)$",
    re.MULTILINE,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def power_by_instance(path: Path) -> dict[str, float]:
    text = path.read_text(errors="replace")
    rows = {name: float(internal) for internal, name in POWER_ROW.findall(text)}
    expected_counts = re.search(r"AES_CTS_POWER buffers=(\d+) dummy_loads=(\d+)", text)
    if not rows or expected_counts is None:
        raise ValueError(f"missing CTS power rows/counts in {path}")
    n_buf, n_dummy = map(int, expected_counts.groups())
    matched_buf = sum(name.startswith("clkbuf") for name in rows)
    matched_dummy = sum(name.startswith("clkload") for name in rows)
    if (matched_buf, matched_dummy) != (n_buf, n_dummy):
        raise ValueError(
            f"incomplete CTS power rows in {path}: matched buffers/dummies "
            f"{matched_buf}/{matched_dummy}, report says {n_buf}/{n_dummy}"
        )
    return rows


def totals(rows: dict[str, float]) -> tuple[float, float, float]:
    dummy = sum(value for name, value in rows.items() if name.startswith("clkload"))
    total = sum(rows.values())
    return total, dummy, total - dummy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cutoff-uw", type=float, default=-1.0)
    parser.add_argument("--hpwl-limit-pct", type=float, default=1.5)
    args = parser.parse_args()

    root = args.experiment_dir.resolve()
    analysis = root / "analysis"
    integrity = {row["artifact"]: row for row in read_tsv(analysis / "candidate_pool.integrity.tsv")}
    pool_path = Path(integrity["candidate_pool"]["path"])
    if sha256(pool_path) != integrity["candidate_pool"]["sha256"]:
        raise ValueError("frozen candidate pool SHA-256 mismatch")

    frozen = read_tsv(analysis / "tier1_greedy_selection.tsv")
    baseline_rows = power_by_instance(analysis / "preflight_baseline.cts_clock_power.log")
    baseline_total, baseline_dummy, baseline_active = totals(baseline_rows)
    output_rows: list[dict[str, object]] = []
    for candidate in frozen:
        candidate_id = candidate["candidate_id"]
        candidate_odb = root / "candidate_odb" / f"{candidate_id}.odb"
        if sha256(candidate_odb) != candidate["candidate_odb_sha256"]:
            raise ValueError(f"frozen ODB SHA-256 mismatch for {candidate_id}")
        powers = power_by_instance(analysis / f"preflight_{candidate_id}.cts_clock_power.log")
        total, dummy, active = totals(powers)
        total_delta = (total - baseline_total) * 1e6
        dummy_delta = (dummy - baseline_dummy) * 1e6
        active_delta = (active - baseline_active) * 1e6
        hpwl = float(candidate["signal_hpwl_delta_pct"])
        objective_pass = total_delta <= args.cutoff_uw and hpwl <= args.hpwl_limit_pct
        output_rows.append({
            "candidate_id": candidate_id,
            "instance": candidate["instance"],
            "signal_hpwl_delta_pct": hpwl,
            "cts_dummy_internal_delta_uw_from_frozen_table": candidate["cts_dummy_internal_delta_uw"],
            "dummy_internal_delta_uw_from_power_rows": dummy_delta,
            "active_buffer_internal_delta_uw": active_delta,
            "total_clock_internal_delta_uw": total_delta,
            "baseline_clock_buffers": sum(name.startswith("clkbuf") for name in baseline_rows),
            "candidate_clock_buffers": sum(name.startswith("clkbuf") for name in powers),
            "baseline_dummy_loads": sum(name.startswith("clkload") for name in baseline_rows),
            "candidate_dummy_loads": sum(name.startswith("clkload") for name in powers),
            "total_internal_objective_pass": int(objective_pass),
            "vcd_full_design_gate": "BLOCKED_PARTIAL_RTL_TRACE",
            "vcd_annotated_pins": "388",
            "vcd_unannotated_pins": "52826",
            "frozen_candidate_odb_sha256": candidate["candidate_odb_sha256"],
        })

    out = args.output or analysis / "total_clock_internal_rescore.tsv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(output_rows[0]), delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Frozen pool SHA-256 verified: {integrity['candidate_pool']['sha256']}")
    print(f"Rescored {len(output_rows)} frozen candidates from CTS per-instance internal-power logs.")
    print(f"Baseline CTS internal power: total={baseline_total * 1e6:.6f} uW "
          f"(dummy={baseline_dummy * 1e6:.6f}, active={baseline_active * 1e6:.6f})")
    print(f"Whole-design VCD gate remains blocked: 388 annotated, 52826 unannotated pins.")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
