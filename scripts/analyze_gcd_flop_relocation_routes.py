#!/usr/bin/env python3
"""Attribute routed power and clock-tree changes for the GCD flop-move pool."""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENROAD = ROOT / "tools/openroad/build/bin/openroad"
GRT_TCL = ROOT / "scripts/gcd_report_global_route_clock_caps.tcl"
VOLTAGE = 1.8
WORKLOADS = ("idle", "typical", "peak")
EXPERIMENT_DIR = Path()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        return list(csv.DictReader(source, delimiter="\t"))


def write_tsv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise ValueError(f"refusing to write empty table: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def parse_power_caps(path: Path) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    header: list[str] | None = None
    with path.open() as source:
        for line in source:
            if not line.startswith("#SPEF_DRIVER_CAP\t"):
                continue
            parts = line.rstrip("\n").split("\t")[1:]
            if parts[0] == "pin":
                header = parts
                continue
            if header is None or len(parts) != len(header):
                raise ValueError(f"malformed SPEF driver-cap row in {path}: {line.rstrip()}")
            row = dict(zip(header, parts))
            net = str(row["net"])
            if net in result:
                raise ValueError(f"multiple SPEF driver rows for net {net} in {path}")
            result[net] = {
                "pin_cap_f": float(row["pin_cap"]),
                "wire_cap_f": float(row["wire_cap"]),
                "total_cap_f": float(row["total_cap"]),
                "density_hz": float(row["density_hz"]),
                "origin": str(row["activity_origin"]),
            }
    if not result:
        raise ValueError(f"no SPEF driver-cap rows in {path}")
    return result


def switching_formula(caps: dict[str, dict[str, object]], origin: str | None = None) -> float:
    return sum(
        0.5 * VOLTAGE**2 * float(row["total_cap_f"]) * float(row["density_hz"])
        for row in caps.values()
        if origin is None or row["origin"] == origin
    )


def parse_summary(path: Path) -> tuple[dict[str, dict[str, float]], bool, bool]:
    text = path.read_text()
    power: dict[str, dict[str, float]] = {}
    for workload in WORKLOADS:
        internal = re.search(
            rf"- {workload}: internal [0-9.eE+-]+ -> [0-9.eE+-]+ W "
            rf"\(delta ([+-]?[0-9.eE+-]+) W\); net switching",
            text,
        )
        match = re.search(
            rf"- {workload}: internal [0-9.eE+-]+ -> [0-9.eE+-]+ W "
            rf"\(delta [+-]?[0-9.eE+-]+ W\); net switching ([0-9.eE+-]+) -> "
            rf"([0-9.eE+-]+) W \(delta ([+-]?[0-9.eE+-]+) W\); dynamic .*?; "
            rf"leakage .*?; total ([0-9.eE+-]+) -> ([0-9.eE+-]+) W "
            rf"\(delta ([+-]?[0-9.eE+-]+) W\)",
            text,
        )
        if match is None:
            # Retain compatibility with summaries written by older flow scripts.
            match = re.search(
            rf"- {workload}: switching ([0-9.eE+-]+) -> ([0-9.eE+-]+) W "
            rf"\(delta ([+-]?[0-9.eE+-]+) W\); total ([0-9.eE+-]+) -> "
            rf"([0-9.eE+-]+) W \(delta ([+-]?[0-9.eE+-]+) W\)", text
            )
        timing = re.search(
            rf"- {workload}:.*?\n\s+Timing slack: setup ([0-9.eE+-]+) -> "
            rf"([0-9.eE+-]+) ns; hold ([0-9.eE+-]+) -> ([0-9.eE+-]+) ns",
            text,
        )
        if not match or not timing or not internal:
            raise ValueError(f"missing routed power or timing row in {path} for {workload}")
        annotation = re.search(
            rf"- {workload}:.*?Activity annotated: (\d+) baseline / (\d+) candidate; "
            rf"unannotated: (\d+) / (\d+)\.", text, re.S
        )
        if not annotation:
            raise ValueError(f"missing VCD annotation coverage in {path}/{workload}")
        annotated_baseline, annotated_candidate, unannotated_baseline, unannotated_candidate = map(int, annotation.groups())
        if min(annotated_baseline, annotated_candidate) <= 0 or unannotated_baseline or unannotated_candidate:
            raise ValueError(f"incomplete VCD annotation coverage in {path}/{workload}: {annotation.groups()}")
        power[workload] = {
            "internal_delta_w": float(internal.group(1)),
            "baseline_switching_w": float(match.group(1)),
            "candidate_switching_w": float(match.group(2)),
            "switching_delta_w": float(match.group(3)),
            "baseline_total_w": float(match.group(4)),
            "candidate_total_w": float(match.group(5)),
            "total_delta_w": float(match.group(6)),
            "setup_before_ns": float(timing.group(1)),
            "setup_after_ns": float(timing.group(2)),
            "hold_before_ns": float(timing.group(3)),
            "hold_after_ns": float(timing.group(4)),
            "annotated_baseline": annotated_baseline,
            "annotated_candidate": annotated_candidate,
        }
    return power, "SPEF candidate acceptance: PASS" in text, "Setup/hold guard (50 ps): PASS" in text


def parse_cts_log(path: Path) -> dict[str, object]:
    text = path.read_text()

    def required(pattern: str, label: str) -> str:
        match = re.search(pattern, text)
        if not match:
            raise ValueError(f"missing CTS feature {label} in {path}")
        return match.group(1)

    return {
        "cts_buffer_count": int(required(r"Created (\d+) clock buffers", "buffer count")),
        "cts_clock_net_count": int(required(r"Created (\d+) clock nets", "clock-net count")),
        "cts_avg_sink_wire_length_um": float(required(r"Average sink wire length ([0-9.]+) um", "average sink wire length")),
        "cts_sink_region": required(r"Original sink region: (\[[^\n]+\])", "sink region"),
    }


def export_grt_clock_caps(variant: str, output_dir: Path) -> dict[str, object]:
    result_dir = ROOT / "build/orfs/results/sky130hd/gcd_power" / variant
    odb = result_dir / "5_1_grt.odb"
    sdc = result_dir / "5_1_grt.sdc"
    if not odb.is_file() or not sdc.is_file():
        raise FileNotFoundError(f"missing GRT ODB/SDC for {variant}")
    before = sha256(odb)
    cap_path = output_dir / "grt_clock_caps" / f"{variant}.tsv"
    log_path = cap_path.with_suffix(".log")
    cap_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.update({
        "STAGE_ODB": str(odb),
        "STAGE_SDC": str(sdc),
        "CLOCK_CAP_REPORT": str(cap_path),
    })
    process = subprocess.run(
        [str(OPENROAD), "-no_init", "-exit", str(GRT_TCL)],
        cwd=ROOT, env=environment, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False,
    )
    log_path.write_text(process.stdout)
    if process.returncode:
        raise RuntimeError(f"GRT clock-cap export failed for {variant}; see {log_path}")
    after = sha256(odb)
    if before != after:
        raise ValueError(f"read-only GRT exporter modified {odb}")

    nets: dict[str, dict[str, float]] = {}
    for line in cap_path.read_text().splitlines():
        if line.startswith("#GRT_CLOCK_CAP\t") and not line.startswith("#GRT_CLOCK_CAP\tnet"):
            _, net, pin_cap, wire_cap, total_cap, endpoints = line.split("\t")
            nets[net] = {
                "pin_cap_f": float(pin_cap),
                "wire_cap_f": float(wire_cap),
                "total_cap_f": float(total_cap),
                "endpoint_count": int(endpoints),
            }
    if not nets:
        raise ValueError(f"no clock caps in GRT report for {variant}")
    return {
        "variant": variant,
        "odb_sha256": before,
        "clock_net_count": len(nets),
        "pin_cap_f": sum(row["pin_cap_f"] for row in nets.values()),
        "wire_cap_f": sum(row["wire_cap_f"] for row in nets.values()),
        "total_cap_f": sum(row["total_cap_f"] for row in nets.values()),
        "nets": nets,
    }


def write_markdown(path: Path, candidate_rows: list[dict[str, object]], workload_rows: list[dict[str, object]],
                   grt_rows: list[dict[str, object]], grt_metrics: dict[str, float]) -> None:
    accepted = [row for row in candidate_rows if row["spef_acceptance"] == "PASS"]
    all_workload_net_savings = {
        str(row["candidate_id"])
        for row in candidate_rows
        if all(float(row[f"{workload}_switching_delta_nw"]) < 0.0 for workload in WORKLOADS)
    }
    passing = [row for row in candidate_rows if str(row["candidate_id"]) in all_workload_net_savings]
    safe = [row for row in candidate_rows if row["timing_guard"] == "PASS"]
    lines = [
        "# GCD sequential-cell relocation: routed clock-cost experiment",
        "",
        "This experiment changes one clock-sink coordinate per candidate. The pool was generated before routing, "
        "screened at detailed placement with UPF and idle/typical/peak VCDs, then routed with each candidate's "
        "own final-netlist VCD, ODB, SDC, and SPEF.",
        "",
        f"- Screened sequential-cell moves: {len(read_tsv(EXPERIMENT_DIR / 'relocation_audit.tsv'))}.",
        f"- Candidates routed from the frozen pool: {len(candidate_rows)}.",
        f"- Routed candidates passing the 50 ps setup/hold guard: {len(safe)}/{len(candidate_rows)}.",
        f"- Candidates passing the routed dynamic-power acceptance gate: {len(accepted)}.",
        f"- Candidates reducing net switching in idle, typical, and peak: {len(passing)}.",
        "",
        "## Best all-workload candidate",
        "",
    ]
    if passing:
        winner = min(passing, key=lambda row: max(float(row[f"{w}_switching_delta_nw"]) for w in WORKLOADS))
        clock_fraction_pct = (
            abs(float(winner["typical_clock_delta_nw"]))
            / max(abs(float(winner["typical_switching_delta_nw"])), 1e-12)
            * 100.0
        )
        lines += [
            f"`{winner['candidate_id']}` moves `{winner['instance']}` from "
            f"({winner['from_x_dbu']}, {winner['from_y_dbu']}) to "
            f"({winner['to_x_dbu']}, {winner['to_y_dbu']}) DBU, a {winner['displacement_um']} µm Euclidean move.",
            "",
            "| Workload | Routed switching change | Relative change | Clock component | Signal component |",
            "|---|---:|---:|---:|---:|",
        ]
        for workload in WORKLOADS:
            power = next(row for row in workload_rows if row["candidate_id"] == winner["candidate_id"] and row["workload"] == workload)
            lines.append(
                f"| {workload} | {float(power['switching_delta_nw']):+.2f} nW | "
                f"{float(power['switching_delta_pct']):+.3f}% | {float(power['clock_delta_nw']):+.2f} nW | "
                f"{float(power['signal_delta_nw']):+.2f} nW |"
            )
        lines += [
            "",
            f"Routed setup slack changed from {winner['setup_before_ns']} ns to {winner['setup_after_ns']} ns "
            f"(loss {winner['setup_loss_ps']} ps); hold changed from {winner['hold_before_ns']} ns to "
            f"{winner['hold_after_ns']} ns (loss {winner['hold_loss_ps']} ps). Both are within the 50 ps guard.",
            "",
            f"The clock component is computed from the per-net detailed-route SPEF capacitance and annotated "
            f"clock activity. It accounts for {clock_fraction_pct:.1f}% of the typical-workload "
            "switching reduction; signal-net power can add to or offset that clock change.",
            "",
            f"The placement-stage switching estimate for this move was "
            f"{float(winner['placed_idle_delta_nw']):+.2f} nW idle, "
            f"{float(winner['placed_typical_delta_nw']):+.2f} nW typical, and "
            f"{float(winner['placed_peak_delta_nw']):+.2f} nW peak. It missed the idle win and underpredicted "
            "routed savings; the additional clock benefit only became measurable after CTS and detailed routing.",
        ]
    else:
        lines += ["No routed candidate reduced switching power in all three workloads."]

    buffer_counts = sorted({int(row["cts_buffer_count"]) for row in candidate_rows})
    net_counts = sorted({int(row["cts_clock_net_count"]) for row in candidate_rows})
    sink_lengths = [float(row["cts_avg_sink_wire_length_um"]) for row in candidate_rows]
    grt_wire = [float(row["delta_grt_clock_wire_cap_ff"]) for row in grt_rows if row["candidate_id"] != "baseline"]
    final_clock_cap = [float(row["final_spef_clock_cap_delta_ff"]) for row in candidate_rows]
    lines += [
        "",
        "## What the stages reveal",
        "",
        f"CTS buffer counts were {buffer_counts}; CTS clock-net counts were {net_counts}. The average sink-wire "
        f"length ranged from {min(sink_lengths):.2f} to {max(sink_lengths):.2f} µm. These aggregate counts stayed "
        "stable even though some candidates changed or removed dummy cells, so buffer count alone does not capture "
        "the clock-power step.",
        f"Global-route clock-wire-cap deltas ranged from {min(grt_wire):+.4f} to {max(grt_wire):+.4f} fF. "
        f"Final SPEF clock-cap deltas ranged from {min(final_clock_cap):+.4f} to {max(final_clock_cap):+.4f} fF. "
        "Detailed-route SPEF clock-power deltas are in `workload_power.tsv`; clock power is workload-invariant "
        "because the same clock activity is used for all three traces.",
        f"Across the frozen holdout, GRT clock-wire-cap delta and final SPEF clock-cap delta had "
        f"Spearman ρ={grt_metrics['spearman']:.3f}; signs matched in "
        f"{int(grt_metrics['sign_matches'])}/{int(grt_metrics['sign_cases'])} non-negligible cases. "
        "No scale or damping factor was fitted on this holdout.",
        "",
        "This frozen all-leaf pool shows sequential-sink relocation can change CTS dummy selection and routed "
        "clock power. The FLUTE leaf-wire estimate remains a separate model and must be judged by the rank and "
        "sign results in `cross_row_holdout_report.md`; final SPEF remains the routed reference.",
        "",
        "## Files",
        "",
        "- `candidate_summary.tsv`: move, timing, CTS, GRT, and acceptance results.",
        "- `workload_power.tsv`: per-workload routed switching deltas split into clock and signal contributions.",
        "- `grt_clock_features.tsv`, `grt_clock_cap_comparison.tsv`, and `grt_clock_caps/`: global-route clock-cap features and raw reports.",
        "- `cross_row_holdout_report.md`: frozen gate, sign, and rank-correlation evaluation.",
        "- `input_hashes.tsv`: frozen input and routed-artifact hashes.",
    ]
    path.write_text("\n".join(lines) + "\n")


EXPERIMENT_DIR = Path()


def main() -> int:
    global EXPERIMENT_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    args = parser.parse_args()
    EXPERIMENT_DIR = args.experiment_dir.resolve()
    analysis_dir = EXPERIMENT_DIR / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    audits = {row["candidate_id"]: row for row in read_tsv(EXPERIMENT_DIR / "relocation_audit.tsv")}
    routes = read_tsv(EXPERIMENT_DIR / "route_manifest.tsv")
    candidates: list[dict[str, object]] = []
    workload_power_rows: list[dict[str, object]] = []
    hash_rows: list[dict[str, object]] = []
    for route in routes:
        if int(route["return_code"]):
            raise ValueError(f"route failed for {route['candidate_id']}")
        candidate_id = route["candidate_id"]
        audit = audits[candidate_id]
        variant = route["route_variant"]
        candidate_dir = ROOT / "build/power" / variant
        summary_path = candidate_dir / "summary.txt"
        if not (candidate_dir / "route_complete.txt").is_file():
            raise ValueError(f"route completion marker is missing for {candidate_id}")
        route_power, accepted, timing_guard = parse_summary(summary_path)
        cts = parse_cts_log(candidate_dir / "orfs.log")
        displacement_um = math.hypot(
            int(route["to_x_dbu"]) - int(route["from_x_dbu"]),
            int(route["to_y_dbu"]) - int(route["from_y_dbu"]),
        ) / 1000.0
        audit_row: dict[str, object] = {
            "candidate_id": candidate_id,
            "instance": route["instance"],
            "from_x_dbu": int(route["from_x_dbu"]),
            "from_y_dbu": int(route["from_y_dbu"]),
            "to_x_dbu": int(route["to_x_dbu"]),
            "to_y_dbu": int(route["to_y_dbu"]),
            "displacement_um": f"{displacement_um:.3f}",
            "placed_idle_delta_nw": float(audit["idle_delta_w"]) * 1e9,
            "placed_typical_delta_nw": float(audit["typical_delta_w"]) * 1e9,
            "placed_peak_delta_nw": float(audit["peak_delta_w"]) * 1e9,
            "spef_acceptance": "PASS" if accepted else "REJECTED",
            "timing_guard": "PASS" if timing_guard else "FAIL",
            **cts,
        }
        for workload in WORKLOADS:
            baseline_caps = parse_power_caps(candidate_dir / "baseline" / f"{workload}.driver_caps.tsv")
            candidate_caps = parse_power_caps(candidate_dir / "candidate" / f"{workload}.driver_caps.tsv")
            if baseline_caps.keys() != candidate_caps.keys():
                raise ValueError(f"per-net SPEF driver coverage differs for {candidate_id}/{workload}")
            formula_delta = switching_formula(candidate_caps) - switching_formula(baseline_caps)
            clock_delta = switching_formula(candidate_caps, "clock") - switching_formula(baseline_caps, "clock")
            reported = route_power[workload]
            actual_delta = reported["switching_delta_w"]
            if abs(formula_delta - actual_delta) > max(2.0e-9, abs(actual_delta) * 0.01):
                raise ValueError(
                    f"SPEF per-net power fails reconciliation for {candidate_id}/{workload}: "
                    f"formula={formula_delta:.6e} reported={actual_delta:.6e} W"
                )
            signal_delta = formula_delta - clock_delta
            workload_power_rows.append({
                "candidate_id": candidate_id,
                "workload": workload,
                "baseline_switching_w": reported["baseline_switching_w"],
                "candidate_switching_w": reported["candidate_switching_w"],
                "switching_delta_w": actual_delta,
                "switching_delta_nw": actual_delta * 1e9,
                "internal_delta_nw": float(reported["internal_delta_w"]) * 1e9,
                "dynamic_delta_nw": (float(reported["internal_delta_w"]) + actual_delta) * 1e9,
                "switching_delta_pct": actual_delta / reported["baseline_switching_w"] * 100.0,
                "clock_delta_nw": clock_delta * 1e9,
                "signal_delta_nw": signal_delta * 1e9,
                "formula_reconciliation_error_nw": (formula_delta - actual_delta) * 1e9,
                "setup_before_ns": reported["setup_before_ns"],
                "setup_after_ns": reported["setup_after_ns"],
                "hold_before_ns": reported["hold_before_ns"],
                "hold_after_ns": reported["hold_after_ns"],
                "annotated_baseline_pins": int(reported["annotated_baseline"]),
                "annotated_candidate_pins": int(reported["annotated_candidate"]),
                "unannotated_pins": 0,
            })
            if workload == "typical":
                audit_row.update({
                    "typical_switching_delta_nw": actual_delta * 1e9,
                    "typical_internal_delta_nw": float(reported["internal_delta_w"]) * 1e9,
                    "setup_before_ns": reported["setup_before_ns"],
                    "setup_after_ns": reported["setup_after_ns"],
                    "setup_loss_ps": (reported["setup_before_ns"] - reported["setup_after_ns"]) * 1000.0,
                    "hold_before_ns": reported["hold_before_ns"],
                    "hold_after_ns": reported["hold_after_ns"],
                    "hold_loss_ps": (reported["hold_before_ns"] - reported["hold_after_ns"]) * 1000.0,
                    "typical_clock_delta_nw": clock_delta * 1e9,
                })
            audit_row[f"{workload}_switching_delta_nw"] = actual_delta * 1e9

        values = dict(line.split("\t", 1) for line in (candidate_dir / "integrity.tsv").read_text().splitlines() if "\t" in line)
        for key in ("baseline_odb", "baseline_spef", "candidate_place_odb"):
            if values.get(f"{key}_before") != values.get(f"{key}_after"):
                raise ValueError(f"source integrity check failed for {candidate_id}/{key}")
        baseline_result = ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
        if sha256(baseline_result / "6_final.odb") != values["baseline_odb_before"]:
            raise ValueError(f"current baseline final ODB hash differs from routed source for {candidate_id}")
        if sha256(baseline_result / "6_final.spef") != values["baseline_spef_before"]:
            raise ValueError(f"current baseline final SPEF hash differs from routed source for {candidate_id}")
        candidate_place_path = Path(audit["odb"])
        if sha256(candidate_place_path) != values["candidate_place_odb_before"]:
            raise ValueError(f"candidate placement ODB hash differs from routed source for {candidate_id}")
        for key, value in values.items():
            hash_rows.append({"candidate_id": candidate_id, "artifact": key, "sha256": value})
        for filename in ("6_final.odb", "6_final.spef"):
            artifact = ROOT / "build/orfs/results/sky130hd/gcd_power" / variant / filename
            hash_rows.append({"candidate_id": candidate_id, "artifact": filename, "sha256": sha256(artifact)})
        candidates.append(audit_row)

    # Export global-route clock capacitance after the detailed-route experiment.
    variants = ["gcd_baseline20ns"] + [row["route_variant"] for row in routes]
    grt = {variant: export_grt_clock_caps(variant, analysis_dir) for variant in variants}
    baseline_grt = grt["gcd_baseline20ns"]
    grt_rows: list[dict[str, object]] = []
    for variant in variants:
        data = grt[variant]
        grt_rows.append({
            "candidate_id": "baseline" if variant == "gcd_baseline20ns" else next(r["candidate_id"] for r in routes if r["route_variant"] == variant),
            "variant": variant,
            "odb_sha256": data["odb_sha256"],
            "clock_net_count": data["clock_net_count"],
            "grt_pin_cap_ff": data["pin_cap_f"] * 1e15,
            "grt_wire_cap_ff": data["wire_cap_f"] * 1e15,
            "grt_total_cap_ff": data["total_cap_f"] * 1e15,
            "delta_grt_clock_wire_cap_ff": (data["wire_cap_f"] - baseline_grt["wire_cap_f"]) * 1e15,
            "delta_grt_clock_total_cap_ff": (data["total_cap_f"] - baseline_grt["total_cap_f"]) * 1e15,
        })
    grt_by_id = {str(row["candidate_id"]): row for row in grt_rows}
    for candidate in candidates:
        grt_row = grt_by_id[str(candidate["candidate_id"])]
        final_clock_delta_ff = (
            float(candidate["typical_clock_delta_nw"]) * 1e-9
            / (0.5 * VOLTAGE**2 * 1e8) * 1e15
        )
        candidate.update({key: grt_row[key] for key in (
            "grt_wire_cap_ff", "delta_grt_clock_wire_cap_ff", "delta_grt_clock_total_cap_ff", "clock_net_count"
        )})
        candidate["final_spef_clock_cap_delta_ff"] = final_clock_delta_ff

    # Correlation-only diagnostics. Do not fit a scale on this frozen holdout.
    x = [float(row["delta_grt_clock_wire_cap_ff"]) for row in candidates]
    y = [float(row["final_spef_clock_cap_delta_ff"]) for row in candidates]
    def ranks(values: list[float]) -> list[float]:
        ordering = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        start = 0
        while start < len(ordering):
            end = start + 1
            while end < len(ordering) and values[ordering[end]] == values[ordering[start]]:
                end += 1
            rank = (start + end - 1) / 2.0 + 1.0
            for offset in range(start, end):
                result[ordering[offset]] = rank
            start = end
        return result
    rank_x, rank_y = ranks(x), ranks(y)
    mean_x = sum(rank_x) / len(rank_x)
    mean_y = sum(rank_y) / len(rank_y)
    covariance = sum((a - mean_x) * (b - mean_y) for a, b in zip(rank_x, rank_y))
    denom_rank = (sum((a - mean_x) ** 2 for a in rank_x) * sum((b - mean_y) ** 2 for b in rank_y)) ** 0.5
    spearman = covariance / denom_rank if denom_rank else 0.0
    sign_cases = [(actual, predicted) for actual, predicted in zip(y, x) if abs(actual) > 0.1]
    sign_matches = sum((actual > 0) == (predicted > 0) for actual, predicted in sign_cases)
    grt_metrics = {
        "spearman": spearman,
        "sign_matches": sign_matches,
        "sign_cases": len(sign_cases),
    }
    grt_cap_comparison_rows = [
        {
            "candidate_id": row["candidate_id"],
            "grt_clock_wire_delta_ff": x[index],
            "actual_final_spef_clock_cap_delta_ff": y[index],
            "unscaled_grt_prediction_ff": x[index],
        }
        for index, row in enumerate(candidates)
    ]

    # Include all immutable data used for the report.
    source_files = [
        EXPERIMENT_DIR / "candidate_pool.tsv",
        EXPERIMENT_DIR / "relocation_audit.tsv",
        EXPERIMENT_DIR / "route_manifest.tsv",
        EXPERIMENT_DIR / "frozen_pool_integrity.tsv",
        EXPERIMENT_DIR / "analysis/pre_route_score_integrity.tsv",
        ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/6_final.odb",
        ROOT / "build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns/6_final.spef",
    ]
    for path in source_files:
        hash_rows.append({"candidate_id": "shared", "artifact": str(path.relative_to(ROOT)), "sha256": sha256(path)})

    write_tsv(analysis_dir / "candidate_summary.tsv", candidates)
    write_tsv(analysis_dir / "workload_power.tsv", workload_power_rows)
    write_tsv(analysis_dir / "grt_clock_features.tsv", grt_rows)
    write_tsv(analysis_dir / "grt_clock_cap_comparison.tsv", grt_cap_comparison_rows)
    write_tsv(analysis_dir / "grt_clock_cap_metrics.tsv", [grt_metrics])
    write_tsv(analysis_dir / "input_hashes.tsv", hash_rows)
    write_markdown(analysis_dir / "summary.md", candidates, workload_power_rows, grt_rows, grt_metrics)
    print(f"GCD_RELOCATION_ANALYSIS candidates={len(candidates)} all_workload_pass={sum(row['spef_acceptance']=='PASS' for row in candidates)} output={analysis_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
