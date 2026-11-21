#!/usr/bin/env python3
"""Build CTS-matched GRT checkpoints and freeze fixed-VCD switching estimates."""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FLOW_DIR = ROOT / "tools/openroad-flow-scripts/flow"
DESIGN_CONFIG = ROOT / "designs/gcd/flow/config.mk"
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
GR_CAP_TCL = ROOT / "scripts/gcd_report_global_route_activity_caps.tcl"
WORKLOADS = ("idle", "typical", "peak")
VOLTAGE = 1.8
WORK_HOME = ROOT / "build/orfs"
RESULTS_ROOT = ROOT / "build/orfs/results/sky130hd/gcd_power"
FIXED_VCD_DIR = ROOT / "build/activity/gcd_baseline20ns/routed"


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write an empty TSV: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
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


def build_grt(variant: str, log_path: Path, num_cores: int) -> None:
    result_dir = RESULTS_ROOT / variant
    cts_odb = result_dir / "4_cts.odb"
    cts_sdc = result_dir / "4_cts.sdc"
    grt_odb = result_dir / "5_1_grt.odb"
    grt_sdc = result_dir / "5_1_grt.sdc"
    if not cts_odb.is_file() or not cts_sdc.is_file():
        raise FileNotFoundError(f"missing CTS checkpoint for {variant}")
    if grt_odb.exists() or grt_sdc.exists():
        raise FileExistsError(f"refusing to reuse existing GRT checkpoint for {variant}")
    command = [
        "make", "-C", str(FLOW_DIR),
        f"DESIGN_CONFIG={DESIGN_CONFIG}",
        "DESIGN_NICKNAME=gcd_power",
        f"FLOW_VARIANT={variant}",
        f"WORK_HOME={WORK_HOME}",
        f"OPENROAD_EXE={OPENROAD}",
        f"NUM_CORES={num_cores}",
        "do-grt",
    ]
    process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(process.stdout)
    if process.returncode or not grt_odb.is_file() or not grt_sdc.is_file():
        raise RuntimeError(f"GRT flow failed for {variant}; see {log_path}")


def export_caps(variant: str, output: Path, log: Path) -> tuple[list[dict[str, str]], str]:
    result_dir = RESULTS_ROOT / variant
    odb = result_dir / "5_1_grt.odb"
    sdc = result_dir / "5_1_grt.sdc"
    env = os.environ.copy()
    env.update({
        "STAGE_ODB": str(odb),
        "STAGE_SDC": str(sdc),
        "IDLE_VCD": str(FIXED_VCD_DIR / "idle.vcd"),
        "TYPICAL_VCD": str(FIXED_VCD_DIR / "typical.vcd"),
        "PEAK_VCD": str(FIXED_VCD_DIR / "peak.vcd"),
        "GRT_ACTIVITY_CAP_REPORT": str(output),
    })
    before = sha256(odb)
    process = subprocess.run([str(OPENROAD), "-no_init", "-exit", str(GR_CAP_TCL)],
                             cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(process.stdout)
    if process.returncode or not output.is_file():
        raise RuntimeError(f"fixed-VCD GRT power export failed for {variant}; see {log}")
    after = sha256(odb)
    if before != after:
        raise ValueError(f"read-only GRT exporter modified {odb}")

    rows: list[dict[str, str]] = []
    with output.open() as source:
        for line in source:
            if not line.startswith("#GRT_DRIVER_CAP\t") or line.rstrip().endswith("\tactivity_origin"):
                continue
            fields = line.rstrip("\n").split("\t")[1:]
            if len(fields) != 10:
                raise ValueError(f"malformed driver cap row in {output}: {line.rstrip()}")
            row = dict(zip(("workload", "class", "pin", "net", "pin_cap_f", "wire_cap_f",
                            "total_cap_f", "density_hz", "duty", "activity_origin"), fields))
            rows.append(row)
    if not rows:
        raise ValueError(f"no driver activity/capacitance rows exported for {variant}")
    return rows, before


def power_by_class(rows: list[dict[str, str]], workload: str) -> dict[str, float]:
    result = {"clock": 0.0, "signal": 0.0}
    for row in rows:
        if row["workload"] != workload:
            continue
        category = row["class"]
        if category not in result:
            raise ValueError(f"unexpected GRT net class {category}")
        # Power = 1/2 * C * V^2 * transition density.
        result[category] += 0.5 * VOLTAGE**2 * float(row["total_cap_f"]) * float(row["density_hz"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--cts-manifest", type=Path, required=True)
    parser.add_argument("--num-cores", type=int, default=1)
    args = parser.parse_args()
    experiment = args.experiment_dir.resolve()
    analysis = experiment / "analysis"
    manifest_path = args.cts_manifest.resolve()
    out_dir = analysis / "grt_fixed_vcd"
    if out_dir.exists():
        raise FileExistsError(f"refusing to overwrite frozen GRT directory {out_dir}")
    out_dir.mkdir(parents=True)

    pool = read_tsv(experiment / "candidate_pool.tsv")
    placement_audit = {row["candidate_id"]: row for row in read_tsv(experiment / "relocation_audit.tsv")}
    cts_rows = read_tsv(manifest_path)
    cts_by_id = {row["candidate_id"]: row for row in cts_rows}
    expected_ids = {"baseline", *(row["candidate_id"] for row in pool)}
    if set(cts_by_id) != expected_ids or any(row.get("status") != "ok" for row in cts_rows):
        raise ValueError("CTS manifest does not exactly cover the frozen pool plus baseline")

    for candidate_id, item in sorted(cts_by_id.items()):
        if candidate_id == "baseline":
            # This is the oracle baseline CTS, matched to every candidate's exact flow settings.
            build_grt(item["variant"], out_dir / f"{candidate_id}.grt.log", args.num_cores)
        else:
            build_grt(item["variant"], out_dir / f"{candidate_id}.grt.log", args.num_cores)

    raw: dict[str, dict[str, list[dict[str, str]]]] = {}
    checkpoint_rows: list[dict[str, object]] = []
    activity_rows: list[dict[str, object]] = []
    for candidate_id, item in sorted(cts_by_id.items()):
        variant = item["variant"]
        raw[candidate_id] = {}
        grt_odb = RESULTS_ROOT / variant / "5_1_grt.odb"
        grt_sdc = RESULTS_ROOT / variant / "5_1_grt.sdc"
        report = out_dir / f"{candidate_id}.tsv"
        log = out_dir / f"{candidate_id}.export.log"
        all_rows, odb_hash = export_caps(variant, report, log)
        for workload in WORKLOADS:
            vcd = FIXED_VCD_DIR / f"{workload}.vcd"
            vcd_hash = sha256(vcd)
            selected_rows = [row for row in all_rows if row["workload"] == workload]
            raw[candidate_id][workload] = selected_rows
            origins: dict[str, int] = {}
            for row in selected_rows:
                origins[row["activity_origin"]] = origins.get(row["activity_origin"], 0) + 1
            activity_rows.append({
                "candidate_id": candidate_id,
                "workload": workload,
                "fixed_vcd": str(FIXED_VCD_DIR / f"{workload}.vcd"),
                "fixed_vcd_sha256": vcd_hash,
                "driver_rows": len(selected_rows),
                "clock_driver_rows": sum(row["class"] == "clock" for row in selected_rows),
                "signal_driver_rows": sum(row["class"] == "signal" for row in selected_rows),
                "activity_origins": ";".join(f"{key}:{origins[key]}" for key in sorted(origins)),
            })
            checkpoint_rows.append({
                "candidate_id": candidate_id,
                "variant": variant,
                "cts_odb_sha256": sha256(Path(item["cts_odb"])),
                "grt_odb": str(grt_odb),
                "grt_odb_sha256": odb_hash,
                "grt_sdc": str(grt_sdc),
                "grt_sdc_sha256": sha256(grt_sdc),
                "workload": workload,
                "report": str(report),
                "report_sha256": sha256(report),
            })

    baseline_power = {workload: power_by_class(raw["baseline"][workload], workload) for workload in WORKLOADS}
    score_rows: list[dict[str, object]] = []
    for candidate_id in sorted(expected_ids - {"baseline"}):
        hpwl = float(placement_audit[candidate_id]["signal_hpwl_delta_pct"])
        per_workload: dict[str, dict[str, float]] = {}
        for workload in WORKLOADS:
            candidate_power = power_by_class(raw[candidate_id][workload], workload)
            deltas = {
                category: (candidate_power[category] - baseline_power[workload][category]) * 1e9
                for category in ("clock", "signal")
            }
            deltas["total"] = deltas["clock"] + deltas["signal"]
            per_workload[workload] = deltas
            score_rows.append({
                "candidate_id": candidate_id,
                "workload": workload,
                "signal_hpwl_delta_pct": hpwl,
                "grt_signal_switching_delta_nw": deltas["signal"],
                "grt_clock_switching_delta_nw": deltas["clock"],
                "grt_total_switching_delta_nw": deltas["total"],
                "activity_source": "fixed_baseline_routed_vcd",
                "parasitics": "post_cts_global_route",
            })
        pass_2a = hpwl <= 0.5 and all(per_workload[w]["total"] <= 0.0 for w in WORKLOADS)
        pass_2b = hpwl <= 0.5 and all(
            per_workload[w]["signal"] <= 0.0 and per_workload[w]["clock"] <= 0.0
            for w in WORKLOADS
        )
        for row in score_rows[-3:]:
            row["tier2a_gate_pass"] = int(pass_2a)
            row["tier2b_gate_pass"] = int(pass_2b)

    write_tsv(analysis / "frozen_grt_scores.tsv", score_rows)
    write_tsv(analysis / "frozen_grt_activity_coverage.tsv", activity_rows)
    write_tsv(analysis / "frozen_grt_checkpoints.tsv", checkpoint_rows)
    print(f"FROZEN_GRT scores={len(score_rows)} candidates={len(pool)} "
          f"tier2a_pass={sum(int(row['tier2a_gate_pass']) for row in score_rows if row['workload']=='typical')} "
          f"tier2b_pass={sum(int(row['tier2b_gate_pass']) for row in score_rows if row['workload']=='typical')} "
          f"output={analysis / 'frozen_grt_scores.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
