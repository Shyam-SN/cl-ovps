# CL-OVPS Scripts Catalog

This directory contains all executable and helper scripts developed during the CL-OVPS project. No scripts have been deleted in order to preserve complete historical and methodological transparency.

## Summary of Categorization
- **Active Flow Scripts (34)**: Production scripts implementing the 4-stage optimization pipeline, CTS restructuring, sign-off verification, and universal runner.
- **Analysis & Diagnostic Scripts (73)**: Scripts used to extract spatial/physical features, evaluate holdouts, audit Liberty models, and verify power/timing.
- **Historical & Experimental Scripts (77)**: Research probes, exploratory ablation passes (e.g. Tier 2 pre-route prediction, exploratory swap sweeps), and prototype drivers preserved for auditability.

## 1. Active Flow Scripts

| Script | Description |
|---|---|
| `build_openroad.sh` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_asap7_multivt_lec.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_clock_leaf_ndr.tcl` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_clock_topology.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_clock_topology.tcl` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_copy_odb.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_cts_power.tcl` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_liberty_audit.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_load_upf.tcl` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3a_multivt.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3b_icg_insertion.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3b_postcts_check.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3c_seq_sizing.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3d_gate_folding.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_pass3e_glitch_diag.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_report.tcl` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_run_gate_vcd.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_sky130_lec_models.py` | Core CL-OVPS flow runner / stage component |
| `cl_ovps_sky130hd_stage23.tcl` | Core CL-OVPS flow runner / stage component |
| `run_aes_gatelevel_activity.py` | Core CL-OVPS flow runner / stage component |
| `run_aes_netlist_lec.py` | Core CL-OVPS flow runner / stage component |
| `run_asap7_gcd_multivt_activity.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_cts.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_gate_lec.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_pass.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_stage1.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_stage4.py` | Core CL-OVPS flow runner / stage component |
| `run_cl_ovps_universal.py` | Core CL-OVPS flow runner / stage component |
| `run_flow.sh` | Core CL-OVPS flow runner / stage component |
| `run_gcd_baseline.sh` | Core CL-OVPS flow runner / stage component |
| `run_gcd_netlist_lec.py` | Core CL-OVPS flow runner / stage component |
| `run_multistage_optimizer.py` | Core CL-OVPS flow runner / stage component |
| `run_power.sh` | Core CL-OVPS flow runner / stage component |
| `setup_pdk.sh` | Core CL-OVPS flow runner / stage component |

## 2. Analysis & Diagnostic Scripts

| Script | Description |
|---|---|
| `analyze_aes_tier1_generalization.py` | Diagnostic and feature analysis utility |
| `analyze_cross_design_placement_score.py` | Diagnostic and feature analysis utility |
| `analyze_cross_design_wirecap_predictor.py` | Diagnostic and feature analysis utility |
| `analyze_cts_grt_cap_error.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_activity_model_comparison.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_cross_row_holdout.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_cross_row_tier2_diagnostics.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_cts_branch_proxy.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_cts_oracle.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_expanded_calibration.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_final_grt_holdout.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_flop_relocation_routes.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_prospective_attribution.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_random_flop_relocation_ranking.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_spatial_holdout.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_spef_coupling_pairs.py` | Diagnostic and feature analysis utility |
| `analyze_gcd_vcd_spef_holdout.py` | Diagnostic and feature analysis utility |
| `analyze_place_spef_features.py` | Diagnostic and feature analysis utility |
| `analyze_placement_cap_predictor.py` | Diagnostic and feature analysis utility |
| `analyze_unified_place_spef_calibration.py` | Diagnostic and feature analysis utility |
| `analyze_vcd_swap_spef_calibration.py` | Diagnostic and feature analysis utility |
| `audit_gcd_cross_row_pool_overlap.py` | Diagnostic and feature analysis utility |
| `audit_gcd_multistage_power.py` | Diagnostic and feature analysis utility |
| `audit_gcd_multistage_transform.tcl` | Diagnostic and feature analysis utility |
| `compare_gcd_cts_oracle_holdout.py` | Diagnostic and feature analysis utility |
| `diagnose_gcd_spatial_features.py` | Diagnostic and feature analysis utility |
| `evaluate_cl_ovps_flop_relocations.tcl` | Diagnostic and feature analysis utility |
| `evaluate_gcd_flop_relocations.tcl` | Diagnostic and feature analysis utility |
| `evaluate_place_moves.tcl` | Diagnostic and feature analysis utility |
| `evaluate_vcd_guided_swaps.tcl` | Diagnostic and feature analysis utility |
| `export_cl_ovps_cts_data.tcl` | Diagnostic and feature analysis utility |
| `export_cl_ovps_inventory.tcl` | Diagnostic and feature analysis utility |
| `export_cl_ovps_pass3d_inventory.tcl` | Diagnostic and feature analysis utility |
| `export_cl_ovps_rows.tcl` | Diagnostic and feature analysis utility |
| `export_clock_tree_levels.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_clock_wire_labels.py` | Diagnostic and feature analysis utility |
| `export_gcd_cts_oracle_data.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_cts_oracle_pool.py` | Diagnostic and feature analysis utility |
| `export_gcd_cts_sink_inventory.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_cts_spatial_features.py` | Diagnostic and feature analysis utility |
| `export_gcd_cts_spatial_geometry.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_dummy_cell_caps.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_grt_activity_caps.py` | Diagnostic and feature analysis utility |
| `export_gcd_odb_verilog.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_placement_rows.tcl` | Diagnostic and feature analysis utility |
| `export_gcd_relocation_inventory.tcl` | Diagnostic and feature analysis utility |
| `export_net_activity_map.py` | Diagnostic and feature analysis utility |
| `export_net_physical_features.tcl` | Diagnostic and feature analysis utility |
| `export_net_timing_criticality.tcl` | Diagnostic and feature analysis utility |
| `export_odb_verilog.tcl` | Diagnostic and feature analysis utility |
| `export_place_netlist.tcl` | Diagnostic and feature analysis utility |
| `export_place_spef_physical_features.py` | Diagnostic and feature analysis utility |
| `export_vcd_swap_calibration_features.py` | Diagnostic and feature analysis utility |
| `export_vcd_swap_inventory.tcl` | Diagnostic and feature analysis utility |
| `report_global_route_capacitance.tcl` | Diagnostic and feature analysis utility |
| `report_place_power.tcl` | Diagnostic and feature analysis utility |
| `report_power.tcl` | Diagnostic and feature analysis utility |
| `report_spef_capacitance.tcl` | Diagnostic and feature analysis utility |
| `report_spef_power.tcl` | Diagnostic and feature analysis utility |
| `report_stage_power.tcl` | Diagnostic and feature analysis utility |
| `summarize_activity_cost.py` | Diagnostic and feature analysis utility |
| `summarize_candidate_move.py` | Diagnostic and feature analysis utility |
| `summarize_gcd_baseline.py` | Diagnostic and feature analysis utility |
| `summarize_gcd_prospective_rank.py` | Diagnostic and feature analysis utility |
| `summarize_gcd_swap_calibration.py` | Diagnostic and feature analysis utility |
| `summarize_move_eval.py` | Diagnostic and feature analysis utility |
| `summarize_multiworkload_moves.py` | Diagnostic and feature analysis utility |
| `summarize_null_repeatability.py` | Diagnostic and feature analysis utility |
| `summarize_place_spef_calibration.py` | Diagnostic and feature analysis utility |
| `summarize_place_spef_calibration_batch.py` | Diagnostic and feature analysis utility |
| `summarize_spef_comparison.py` | Diagnostic and feature analysis utility |
| `summarize_spef_net_attribution.py` | Diagnostic and feature analysis utility |
| `summarize_vcd_guided_swap_audit.py` | Diagnostic and feature analysis utility |

## 3. Historical & Experimental Scripts

| Script | Description |
|---|---|
| `aes_report_clock_cell_power.tcl` | Historical calibration / exploratory ablation script |
| `aes_report_spef_power.tcl` | Historical calibration / exploratory ablation script |
| `apply_best_place_move.tcl` | Historical calibration / exploratory ablation script |
| `apply_calibration_move.tcl` | Historical calibration / exploratory ablation script |
| `apply_generic_clock_sink_moves.tcl` | Historical calibration / exploratory ablation script |
| `asap7_gcd_multivt_activity_tb.v` | Historical calibration / exploratory ablation script |
| `calibrate_gcd_cts_clock_cost.py` | Historical calibration / exploratory ablation script |
| `classify_gcd_tiered_power_candidates.py` | Historical calibration / exploratory ablation script |
| `continue_aes_tier1_generalization.py` | Historical calibration / exploratory ablation script |
| `freeze_gcd_cross_row_pool.py` | Historical calibration / exploratory ablation script |
| `freeze_gcd_cts_spatial_score.py` | Historical calibration / exploratory ablation script |
| `freeze_gcd_final_grt_predictions.py` | Historical calibration / exploratory ablation script |
| `freeze_gcd_grt_holdout_scores.py` | Historical calibration / exploratory ablation script |
| `freeze_gcd_vcd_opposite_score.py` | Historical calibration / exploratory ablation script |
| `gcd_report_cts_cell_power.tcl` | Historical calibration / exploratory ablation script |
| `gcd_report_global_route_activity_caps.tcl` | Historical calibration / exploratory ablation script |
| `gcd_report_global_route_clock_caps.tcl` | Historical calibration / exploratory ablation script |
| `gcd_report_place_power.tcl` | Historical calibration / exploratory ablation script |
| `gcd_report_placed_cts_power.tcl` | Historical calibration / exploratory ablation script |
| `gcd_report_spef_power.tcl` | Historical calibration / exploratory ablation script |
| `gcd_tier1_greedy_optimizer.py` | Historical calibration / exploratory ablation script |
| `load_single_domain_upf.tcl` | Historical calibration / exploratory ablation script |
| `make_cl_ovps_hybrid_torture_odb.tcl` | Historical calibration / exploratory ablation script |
| `make_cl_ovps_relocation_pool.py` | Historical calibration / exploratory ablation script |
| `make_gcd_branch_targeted_relocation_pool.py` | Historical calibration / exploratory ablation script |
| `make_gcd_cross_row_pool.py` | Historical calibration / exploratory ablation script |
| `make_gcd_flop_relocation_pool.py` | Historical calibration / exploratory ablation script |
| `make_gcd_random_flop_relocation_pool.py` | Historical calibration / exploratory ablation script |
| `prepare_aes_reconstrained_baseline.py` | Historical calibration / exploratory ablation script |
| `prepare_move_eval_inputs.py` | Historical calibration / exploratory ablation script |
| `prepare_vcd_guided_swaps.py` | Historical calibration / exploratory ablation script |
| `profile_aes_netlist.py` | Historical calibration / exploratory ablation script |
| `recalibrate_vcd_clock_placement_score.py` | Historical calibration / exploratory ablation script |
| `rescore_frozen_aes_total_clock_internal.py` | Historical calibration / exploratory ablation script |
| `run_aes_multistage_pipeline.py` | Historical calibration / exploratory ablation script |
| `run_aes_power_activity.py` | Historical calibration / exploratory ablation script |
| `run_aes_power_reports.py` | Historical calibration / exploratory ablation script |
| `run_aes_tier1_chain_probe.py` | Historical calibration / exploratory ablation script |
| `run_aes_tier1_generalization.py` | Historical calibration / exploratory ablation script |
| `run_aes_tier1_powersearch.py` | Historical calibration / exploratory ablation script |
| `run_best_move_candidate.sh` | Historical calibration / exploratory ablation script |
| `run_candidate_spef_flow.sh` | Historical calibration / exploratory ablation script |
| `run_cts_grt_power_calibration.py` | Historical calibration / exploratory ablation script |
| `run_gcd_candidate_spef_flow.sh` | Historical calibration / exploratory ablation script |
| `run_gcd_cts_oracle.py` | Historical calibration / exploratory ablation script |
| `run_gcd_flop_relocation_routes.py` | Historical calibration / exploratory ablation script |
| `run_gcd_flop_relocation_screen.sh` | Historical calibration / exploratory ablation script |
| `run_gcd_frozen_grt_holdout_routes.py` | Historical calibration / exploratory ablation script |
| `run_gcd_multistage_optimizer.py` | Historical calibration / exploratory ablation script |
| `run_gcd_netlist_differential.py` | Historical calibration / exploratory ablation script |
| `run_null_move_repeatability.sh` | Historical calibration / exploratory ablation script |
| `run_place_move_eval.sh` | Historical calibration / exploratory ablation script |
| `run_place_power.sh` | Historical calibration / exploratory ablation script |
| `run_place_spef_calibration.sh` | Historical calibration / exploratory ablation script |
| `run_place_spef_calibration_batch.sh` | Historical calibration / exploratory ablation script |
| `run_place_spef_calibration_expansion.sh` | Historical calibration / exploratory ablation script |
| `run_spef_net_attribution.sh` | Historical calibration / exploratory ablation script |
| `run_vcd_guided_swap_audit.sh` | Historical calibration / exploratory ablation script |
| `run_vcd_swap_spef_calibration.sh` | Historical calibration / exploratory ablation script |
| `score_gcd_cross_row_holdout.py` | Historical calibration / exploratory ablation script |
| `score_gcd_cts_oracle_pool.py` | Historical calibration / exploratory ablation script |
| `score_gcd_frozen_tier1_dummy.py` | Historical calibration / exploratory ablation script |
| `score_gcd_prospective_candidates.py` | Historical calibration / exploratory ablation script |
| `select_vcd_power_candidates.py` | Historical calibration / exploratory ablation script |
| `stage2_clock_restructurer.tcl` | Historical calibration / exploratory ablation script |
| `stage3_vcd_opt.tcl` | Historical calibration / exploratory ablation script |
| `stage3a_multivt.tcl` | Historical calibration / exploratory ablation script |
| `stage3b_icg_extract.tcl` | Historical calibration / exploratory ablation script |
| `stage3b_icg_insertion.tcl` | Historical calibration / exploratory ablation script |
| `stage3b_icg_postcts_check.tcl` | Historical calibration / exploratory ablation script |
| `stage3c_seq_sizing.tcl` | Historical calibration / exploratory ablation script |
| `stage3d_fold_trial.tcl` | Historical calibration / exploratory ablation script |
| `stage3e_glitch_diag.tcl` | Historical calibration / exploratory ablation script |
| `test_clock_topology_safety.py` | Historical calibration / exploratory ablation script |
| `verify_place_candidate.tcl` | Historical calibration / exploratory ablation script |
| `web_aes_power_compare_read_vcd.tcl` | Historical calibration / exploratory ablation script |
| `write_cl_ovps_regression_manifest.py` | Historical calibration / exploratory ablation script |
