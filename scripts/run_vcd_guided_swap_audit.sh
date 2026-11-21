#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
MOVE_DIR="${ROOT_DIR}/build/power/move_eval"
TAG="${VCD_SWAP_AUDIT_TAG:-$(date +%Y%m%d_%H%M%S)}"
AUDIT_DIR="${ROOT_DIR}/build/power/vcd_swap_audit_${TAG}"
POOL_LIMIT="${VCD_SWAP_AUDIT_POOL_PAIRS:-200}"
SHORTLIST_LIMIT="${VCD_SWAP_AUDIT_SHORTLIST:-24}"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" ]]; then
  echo "Missing OpenROAD build or detailed-placement baseline." >&2
  exit 1
fi
if [[ -e "${AUDIT_DIR}" ]]; then
  echo "Audit output already exists: ${AUDIT_DIR}; choose a fresh VCD_SWAP_AUDIT_TAG." >&2
  exit 1
fi

mkdir -p "${AUDIT_DIR}/activity" "${AUDIT_DIR}/candidate_odb" "${AUDIT_DIR}/physical_features"
BASELINE_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_place_odb_sha256_before\t%s\n' "${BASELINE_SHA}" > "${AUDIT_DIR}/integrity.tsv"

# Keep the VCD-derived activity inputs beside the results so reports can be
# regenerated without relying on the original calibration directory.
for workload in idle typical peak; do
  cp "${MOVE_DIR}/activity/${workload}.active_net_density.tsv" \
    "${AUDIT_DIR}/activity/${workload}.active_net_density.tsv"
done

PLACEMENT_ODB="${BASELINE_RESULT_DIR}/3_5_place_dp.odb" \
  SWAP_INVENTORY_OUT="${AUDIT_DIR}/placed_instances.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_vcd_swap_inventory.tcl" \
  > "${AUDIT_DIR}/inventory_export.log" 2>&1

# Generate a broad pool, then use OpenROAD's placement power and timing
# evaluation to rank actual exchanges instead of taking the first active cells.
python3 "${ROOT_DIR}/scripts/prepare_vcd_guided_swaps.py" \
  --inventory "${AUDIT_DIR}/placed_instances.tsv" \
  --idle-cost-csv "${ROOT_DIR}/build/power/detailed_place/idle.per_net_cost.csv" \
  --typical-cost-csv "${ROOT_DIR}/build/power/detailed_place/typical.per_net_cost.csv" \
  --peak-cost-csv "${ROOT_DIR}/build/power/detailed_place/peak.per_net_cost.csv" \
  --exclude-candidate-file "${MOVE_DIR}/candidate_cells.tsv" \
  --exclude-batch-dir "${ROOT_DIR}/build/power/place_spef_batch_20261027_002801" \
  --exclude-batch-dir "${ROOT_DIR}/build/power/place_spef_expansion_20261027_calset01a" \
  --output "${AUDIT_DIR}/candidate_pool.tsv" \
  --summary "${AUDIT_DIR}/candidate_generation.txt" \
  --max-pairs "${POOL_LIMIT}"

pool_count="$(awk 'END { print NR - 1 }' "${AUDIT_DIR}/candidate_pool.tsv")"
if [[ "${pool_count}" -lt 1 ]]; then
  echo "No same-master, same-row high/low activity swap pairs were available." >&2
  exit 2
fi

env ACTIVITY_VCD="${ROOT_DIR}/build/activity/detailed_place/typical.vcd" \
  MOVE_EVAL_NET_ACTIVITY="${MOVE_DIR}/activity/typical.active_net_density.tsv" \
  MOVE_EVAL_WORKLOAD=typical \
  SWAP_CANDIDATES="${AUDIT_DIR}/candidate_pool.tsv" \
  SWAP_AUDIT_OUTPUT="${AUDIT_DIR}/audit_pool_typical.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/evaluate_vcd_guided_swaps.tcl" \
  > "${AUDIT_DIR}/evaluator_pool_typical.log" 2>&1

python3 "${ROOT_DIR}/scripts/select_vcd_power_candidates.py" \
  --candidates "${AUDIT_DIR}/candidate_pool.tsv" \
  --typical-audit "${AUDIT_DIR}/audit_pool_typical.tsv" \
  --output "${AUDIT_DIR}/candidate_swaps.tsv" \
  --summary "${AUDIT_DIR}/candidate_selection.txt" \
  --max-candidates "${SHORTLIST_LIMIT}" \
  --minimum-improvement-nw "${VCD_SWAP_AUDIT_MIN_IMPROVEMENT_NW:-1}"

shortlist_count="$(awk 'END { print NR - 1 }' "${AUDIT_DIR}/candidate_swaps.tsv")"
if [[ "${shortlist_count}" -eq 0 ]]; then
  AFTER_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
  printf 'baseline_place_odb_sha256_after\t%s\n' "${AFTER_SHA}" >> "${AUDIT_DIR}/integrity.tsv"
  if [[ "${BASELINE_SHA}" != "${AFTER_SHA}" ]]; then
    echo "Baseline placement ODB changed during candidate-pool evaluation." >&2
    exit 4
  fi
  python3 "${ROOT_DIR}/scripts/summarize_vcd_guided_swap_audit.py" \
    --audit-dir "${AUDIT_DIR}" --pool-only
  echo "No typical-workload power-reducing candidates passed the placement timing guard."
  echo "Audit complete: ${AUDIT_DIR}"
  exit 0
fi

for workload in idle typical peak; do
  if [[ "${workload}" == "typical" ]]; then
    env ACTIVITY_VCD="${ROOT_DIR}/build/activity/detailed_place/${workload}.vcd" \
      MOVE_EVAL_NET_ACTIVITY="${MOVE_DIR}/activity/${workload}.active_net_density.tsv" \
      MOVE_EVAL_WORKLOAD="${workload}" \
      SWAP_CANDIDATES="${AUDIT_DIR}/candidate_swaps.tsv" \
      SWAP_AUDIT_OUTPUT="${AUDIT_DIR}/audit_${workload}.tsv" \
      SWAP_ODB_DIR="${AUDIT_DIR}/candidate_odb" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/evaluate_vcd_guided_swaps.tcl" \
      > "${AUDIT_DIR}/evaluator_${workload}.log" 2>&1
  else
    env ACTIVITY_VCD="${ROOT_DIR}/build/activity/detailed_place/${workload}.vcd" \
      MOVE_EVAL_NET_ACTIVITY="${MOVE_DIR}/activity/${workload}.active_net_density.tsv" \
      MOVE_EVAL_WORKLOAD="${workload}" \
      SWAP_CANDIDATES="${AUDIT_DIR}/candidate_swaps.tsv" \
      SWAP_AUDIT_OUTPUT="${AUDIT_DIR}/audit_${workload}.tsv" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/evaluate_vcd_guided_swaps.tcl" \
      > "${AUDIT_DIR}/evaluator_${workload}.log" 2>&1
  fi
done

python3 "${ROOT_DIR}/scripts/select_vcd_power_candidates.py" \
  --candidates "${AUDIT_DIR}/candidate_swaps.tsv" \
  --all-workloads-audit-dir "${AUDIT_DIR}" \
  --output "${AUDIT_DIR}/route_candidates.tsv" \
  --summary "${AUDIT_DIR}/route_candidate_selection.txt" \
  --minimum-improvement-nw "${VCD_SWAP_AUDIT_MIN_IMPROVEMENT_NW:-1}"

FEATURE_ODB="${BASELINE_RESULT_DIR}/3_5_place_dp.odb" \
  FEATURE_STAGE=placement \
  FEATURE_NET_CSV="${AUDIT_DIR}/physical_features/baseline_placement.nets.csv" \
  FEATURE_LAYER_CSV="${AUDIT_DIR}/physical_features/baseline_placement.layers.csv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_net_physical_features.tcl" \
  > "${AUDIT_DIR}/physical_features/baseline_placement.log" 2>&1

route_candidate_ids="${AUDIT_DIR}/route_candidate_ids.txt"
awk -F '\t' 'NR > 1 {print $1}' "${AUDIT_DIR}/route_candidates.tsv" > "${route_candidate_ids}"
for candidate_odb in "${AUDIT_DIR}"/candidate_odb/*.odb; do
  [[ -s "${candidate_odb}" ]] || continue
  pair_id="$(basename "${candidate_odb}" .odb)"
  before_hash="$(shasum -a 256 "${candidate_odb}" | awk '{print $1}')"
  if grep -Fxq "${pair_id}" "${route_candidate_ids}"; then
    FEATURE_ODB="${candidate_odb}" FEATURE_STAGE=placement \
      FEATURE_NET_CSV="${AUDIT_DIR}/physical_features/${pair_id}_placement.nets.csv" \
      FEATURE_LAYER_CSV="${AUDIT_DIR}/physical_features/${pair_id}_placement.layers.csv" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_net_physical_features.tcl" \
      > "${AUDIT_DIR}/physical_features/${pair_id}_placement.log" 2>&1
  fi
  after_hash="$(shasum -a 256 "${candidate_odb}" | awk '{print $1}')"
  if [[ "${before_hash}" != "${after_hash}" ]]; then
    echo "Candidate placement ODB changed during feature export: ${pair_id}" >&2
    exit 3
  fi
  printf 'candidate_%s_odb_sha256_before_after\t%s\n' "${pair_id}" "${before_hash}" \
    >> "${AUDIT_DIR}/integrity.tsv"
done

AFTER_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_place_odb_sha256_after\t%s\n' "${AFTER_SHA}" >> "${AUDIT_DIR}/integrity.tsv"
if [[ "${BASELINE_SHA}" != "${AFTER_SHA}" ]]; then
  echo "Baseline placement ODB changed during VCD swap audit." >&2
  exit 4
fi

python3 "${ROOT_DIR}/scripts/summarize_vcd_guided_swap_audit.py" --audit-dir "${AUDIT_DIR}"
echo "VCD-guided swap audit complete: ${AUDIT_DIR}"
echo "Candidates ready for routed SPEF evaluation: $(awk 'END { print NR - 1 }' "${AUDIT_DIR}/route_candidates.tsv")"
echo "Baseline placement ODB SHA-256 unchanged: ${AFTER_SHA}"
