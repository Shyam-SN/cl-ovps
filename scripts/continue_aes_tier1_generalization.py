#!/usr/bin/env python3
"""Resume AES CTS/GRT holdout analysis from completed frozen GRT checkpoints."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import run_aes_tier1_generalization as aes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-dir", type=Path, required=True)
    parser.add_argument("--cores", type=int, default=2)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    output = args.experiment_dir.resolve()
    analysis = output / "analysis"
    pool = analysis / "candidate_pool.tsv"
    move_audit = analysis / "move_audit.tsv"
    saved = [row for row in aes.read_tsv(move_audit) if row["status"] == "saved"]
    if len(saved) < 4:
        raise RuntimeError(f"only {len(saved)} saved AES placement candidates in {move_audit}")
    candidate_variants = {row["candidate_id"]: f"aes_tier1gen_20261031_{row['candidate_id']}" for row in saved}
    for candidate, variant in candidate_variants.items():
        if not (aes.RESULTS / variant / "5_1_grt.odb").is_file():
            raise FileNotFoundError(f"missing completed candidate GRT checkpoint: {variant}")

    base_sinks = analysis / "baseline_sinks.tsv"
    base_cells = analysis / "baseline_cells.tsv"
    cts_inventory: dict[str, tuple[Path, Path]] = {"baseline": (base_sinks, base_cells)}
    for candidate, variant in candidate_variants.items():
        cts_inventory[candidate] = aes.export_cts(aes.RESULTS / variant / "4_1_cts.odb", analysis, candidate)

    all_variants = {"baseline": aes.BASE_TAG, **candidate_variants}
    grt_caps = {
        candidate: aes.cap_export(variant, "grt", analysis / f"{candidate}.grt_caps.tsv",
                                  analysis / f"{candidate}.grt_caps.log")
        for candidate, variant in all_variants.items()
    }
    base_sink_map = {row["instance"]: row["branch"] for row in aes.read_tsv(base_sinks)}
    base_dummy_map = {row["instance"]: row["master"] for row in aes.read_tsv(base_cells)
                      if row["instance"].startswith("clkload")}
    move_by_id = {row["candidate_id"]: row for row in aes.read_tsv(pool)}
    prediction_rows: list[dict[str, object]] = []
    for candidate, variant in candidate_variants.items():
        sink_file, cell_file = cts_inventory[candidate]
        sink_map = {row["instance"]: row["branch"] for row in aes.read_tsv(sink_file)}
        dummy_map = {row["instance"]: row["master"] for row in aes.read_tsv(cell_file)
                     if row["instance"].startswith("clkload")}
        changed_dummies = sorted(name for name in base_dummy_map.keys() | dummy_map.keys()
                                 if base_dummy_map.get(name) != dummy_map.get(name))
        branch_changes = sum(base_sink_map.get(name) != sink_map.get(name)
                             for name in base_sink_map.keys() | sink_map.keys())
        proposal = move_by_id[candidate]
        prediction_rows.append({
            "candidate_id": candidate, "instance": proposal["instance"],
            "from_x_dbu": proposal["from_x_dbu"], "from_y_dbu": proposal["from_y_dbu"],
            "target_x_dbu": proposal["target_x_dbu"], "target_y_dbu": proposal["target_y_dbu"],
            "move_type": proposal["move_type"], "cts_sink_count": len(sink_map),
            "cts_leaf_count": len(set(sink_map.values())), "cts_sink_branch_changes": branch_changes,
            "dummy_master_changes": ";".join(
                f"{name}:{base_dummy_map.get(name, 'ABSENT')}->{dummy_map.get(name, 'ABSENT')}"
                for name in changed_dummies),
            "dummy_master_change_count": len(changed_dummies),
            "grt_signal_cap_delta_ff": (grt_caps[candidate]["signal"] - grt_caps["baseline"]["signal"]) * 1e15,
            "grt_clock_cap_delta_ff": (grt_caps[candidate]["clock"] - grt_caps["baseline"]["clock"]) * 1e15,
            "candidate_flow_variant": variant,
        })

    predictions = analysis / "frozen_grt_cap_predictions.tsv"
    aes.write_tsv(predictions, prediction_rows)
    generic_generator = analysis / "make_aes_cross_row_pool.py"
    baseline_dir = aes.RESULTS / aes.BASE_TAG
    baseline_place = baseline_dir / "3_5_place_dp.odb"
    baseline_sdc = baseline_dir / "3_place.sdc"
    baseline_cts = baseline_dir / "4_1_cts.odb"
    baseline_grt = baseline_dir / "5_1_grt.odb"
    source_paths = [aes.CONFIG, aes.LIBERTY, aes.GENERATOR, generic_generator, aes.INVENTORY_TCL,
                    aes.ROWS_TCL, aes.CTS_EXPORT_TCL, aes.MOVE_TCL, aes.GRT_CAP_TCL,
                    aes.SPEF_CAP_TCL, aes.OPENROAD, Path(__file__),
                    ROOT / "scripts/run_aes_tier1_generalization.py",
                    ROOT / "tools/openroad-flow-scripts/flow/Makefile",
                    baseline_place, baseline_sdc, baseline_cts, baseline_grt, pool, move_audit, predictions]
    for path in output.joinpath("candidate_odb").glob("*.odb"):
        source_paths.append(path)
    for variant in candidate_variants.values():
        result_dir = aes.RESULTS / variant
        # The original candidate ODB is the placement source; variant-local
        # duplicate placement files may be regenerated by ORFS finish targets.
        source_paths.extend([result_dir / "4_1_cts.odb", result_dir / "5_1_grt.odb"])
    hashes = [{"path": str(path.resolve()), "sha256": aes.sha256(path), "size_bytes": path.stat().st_size}
              for path in sorted({Path(path).resolve() for path in source_paths if Path(path).is_file()})]
    aes.write_tsv(analysis / "frozen_prediction_integrity.tsv", hashes)
    aes.write_tsv(analysis / "pre_detail_route_freeze.tsv", [
        {"metric": "frozen_before_detailed_route", "value": "YES"},
        {"metric": "candidate_count", "value": len(prediction_rows)},
        {"metric": "prediction_sha256", "value": aes.sha256(predictions)},
        {"metric": "integrity_manifest_sha256", "value": aes.sha256(analysis / "frozen_prediction_integrity.tsv")},
    ])

    # Use dependency-based targets so detail route starts from each frozen GRT ODB.
    original_makefile = analysis / "orfs_Makefile.original"
    flow_makefile = ROOT / "tools/openroad-flow-scripts/flow/Makefile"
    if not original_makefile.is_file():
        raise FileNotFoundError(original_makefile)
    if flow_makefile.read_bytes() != original_makefile.read_bytes():
        shutil.copy2(original_makefile, flow_makefile)
        raise RuntimeError("restored the ORFS Makefile; rerun to freeze against the canonical flow file")

    def route_variant(variant: str) -> None:
        # Invoke only the already-frozen route/finish stages. The make-level
        # `route` target can rebuild earlier flow steps for incomplete variants.
        result_dir = aes.RESULTS / variant
        if (result_dir / "6_final.spef").is_file() and (result_dir / "6_final.odb").is_file():
            return
        aes.run_make(variant, ["do-5_2_route", "do-5_3_fillcell", "do-5_route",
                               "do-5_route.sdc", "do-6_1_fill", "do-6_1_fill.sdc",
                               "do-6_report"],
                     output / f"{variant}.detail_route.log", args.cores)

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(route_variant, variant): variant for variant in all_variants.values()}
        for future in as_completed(futures):
            future.result()
    (analysis / "orfs_Makefile_restored.sha256").write_text(
        hashlib.sha256(flow_makefile.read_bytes()).hexdigest() + "  restored-original-Makefile\n")

    base_spef = aes.cap_export(aes.BASE_TAG, "spef", analysis / "baseline.spef_caps.tsv",
                               analysis / "baseline.spef_caps.log")
    result_rows: list[dict[str, object]] = []
    grt_signal: list[float] = []
    route_signal: list[float] = []
    grt_clock: list[float] = []
    route_clock: list[float] = []
    for row in prediction_rows:
        candidate = str(row["candidate_id"])
        variant = str(row["candidate_flow_variant"])
        route_caps = aes.cap_export(variant, "spef", analysis / f"{candidate}.spef_caps.tsv",
                                    analysis / f"{candidate}.spef_caps.log")
        actual_signal = (route_caps["signal"] - base_spef["signal"]) * 1e15
        actual_clock = (route_caps["clock"] - base_spef["clock"]) * 1e15
        row.update({"routed_signal_cap_delta_ff": actual_signal, "routed_clock_cap_delta_ff": actual_clock})
        result_rows.append(row)
        grt_signal.append(float(row["grt_signal_cap_delta_ff"]))
        route_signal.append(actual_signal)
        grt_clock.append(float(row["grt_clock_cap_delta_ff"]))
        route_clock.append(actual_clock)
    aes.write_tsv(analysis / "grt_vs_spef_capacitance.tsv", result_rows)
    from analyze_gcd_cts_branch_proxy import kendall_tau_b, spearman
    correlations = [
        {"component": "signal_cap_delta", "n": len(result_rows),
         "spearman_rho": spearman(grt_signal, route_signal), "kendall_tau_b": kendall_tau_b(grt_signal, route_signal)},
        {"component": "clock_cap_delta", "n": len(result_rows),
         "spearman_rho": spearman(grt_clock, route_clock), "kendall_tau_b": kendall_tau_b(grt_clock, route_clock)},
    ]
    aes.write_tsv(analysis / "correlation_metrics.tsv", correlations)
    dummy_events = sum(int(row["dummy_master_change_count"]) > 0 for row in result_rows)
    (analysis / "report.md").write_text(
        "# AES/Sky130HD Tier-1 and GRT generalization probe\n\n"
        f"CTS sinks in baseline: {len(base_sink_map)} across {len(set(base_sink_map.values()))} leaves.\n"
        f"Isolated cross-row trials routed: {len(result_rows)}.\n"
        f"Candidates with at least one CTS dummy-master change: {dummy_events}.\n\n"
        "GRT capacitance predictions were frozen before detailed routing. The post-route comparison uses total "
        "signal and clock capacitance from SPEF; no AES workload VCD was available, so this is a physical-capacitance "
        "generalization check, not a dynamic-power result.\n\n"
        f"Signal-cap GRT vs SPEF Spearman rho: {correlations[0]['spearman_rho']:.3f}; "
        f"Kendall tau-b: {correlations[0]['kendall_tau_b']:.3f}.\n"
        f"Clock-cap GRT vs SPEF Spearman rho: {correlations[1]['spearman_rho']:.3f}; "
        f"Kendall tau-b: {correlations[1]['kendall_tau_b']:.3f}.\n"
    )
    print(f"AES_TIER1_GENERALIZATION candidates={len(result_rows)} dummy_events={dummy_events} "
          f"signal_rho={correlations[0]['spearman_rho']:.3f} output={analysis / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
