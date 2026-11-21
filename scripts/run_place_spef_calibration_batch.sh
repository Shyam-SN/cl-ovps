#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
MOVE_EVAL_DIR="${ROOT_DIR}/build/power/move_eval"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
TAG="${PLACE_SPEF_BATCH_TAG:-$(date +%Y%m%d_%H%M%S)}"
BATCH_DIR="${ROOT_DIR}/build/power/place_spef_batch_${TAG}"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" ]]; then
  echo "Missing OpenROAD build or detailed-placement baseline." >&2
  exit 1
fi
if [[ -e "${BATCH_DIR}" ]]; then
  echo "Batch output already exists: ${BATCH_DIR}; choose a fresh PLACE_SPEF_BATCH_TAG." >&2
  exit 1
fi
mkdir -p "${BATCH_DIR}"
BASELINE_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'instance\tdx_dbu\tplacement\troute_variant\tcalibration_report\trouted_acceptance\tcomparison_exit_status\n' > "${BATCH_DIR}/batch_manifest.tsv"

# Diverse calibration points: a small predicted regression, a small predicted
# reduction, and a near-zero idle prediction from three different cells.
instances=("_12131_" "place540" "place544")
deltas=("-460" "-460" "460")
for index in "${!instances[@]}"; do
  instance="${instances[$index]}"
  dx="${deltas[$index]}"
  if [[ "${dx}" == -* ]]; then direction="m${dx#-}"; else direction="p${dx}"; fi
  key="${instance}_${direction}"
  candidate_dir="${BATCH_DIR}/${key}"
  candidate_odb="${candidate_dir}/3_5_place_dp.odb"
  move_summary="${candidate_dir}/applied_move.tsv"
  variant="placecal_${TAG}_${key}"
  mkdir -p "${candidate_dir}"

  echo "Preparing ${key} from the unchanged baseline placement."
  ACTIVITY_VCD="${ROOT_DIR}/build/activity/detailed_place/typical.vcd" \
    MOVE_EVAL_DIR="${MOVE_EVAL_DIR}" \
    MOVE_SELECTED_INSTANCE="${instance}" MOVE_SELECTED_DX="${dx}" \
    CANDIDATE_ODB="${candidate_odb}" MOVE_APPLY_SUMMARY="${move_summary}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/apply_calibration_move.tcl" \
    > "${candidate_dir}/apply.log" 2>&1

  comparison_status=0
  MOVE_CANDIDATE_ODB="${candidate_odb}" CANDIDATE_FLOW_VARIANT="${variant}" \
    "${ROOT_DIR}/scripts/run_candidate_spef_flow.sh" > "${candidate_dir}/route_driver.log" 2>&1 || comparison_status=$?
  if [[ ! -s "${ROOT_DIR}/build/power/${variant}/route_complete.txt" ]]; then
    echo "Routing did not complete for ${key}; see ${candidate_dir}/route_driver.log" >&2
    exit 2
  fi

  SPEF_FLOW_VARIANT="${variant}" "${ROOT_DIR}/scripts/run_spef_net_attribution.sh" \
    > "${candidate_dir}/attribution_driver.log" 2>&1
  CANDIDATE_ODB="${candidate_odb}" \
    ROUTED_REPORT_DIR="${ROOT_DIR}/build/power/${variant}" \
    MOVE_APPLY_SUMMARY="${move_summary}" \
    PLACE_SPEF_CAL_TAG="${TAG}_${key}" \
    "${ROOT_DIR}/scripts/run_place_spef_calibration.sh" \
    > "${candidate_dir}/placement_calibration_driver.log" 2>&1

  routed_acceptance="unknown"
  if grep -q 'SPEF candidate acceptance: PASS' "${ROOT_DIR}/build/power/${variant}/summary.txt"; then
    routed_acceptance="PASS"
  elif grep -q 'SPEF candidate acceptance: REJECTED' "${ROOT_DIR}/build/power/${variant}/summary.txt"; then
    routed_acceptance="REJECTED"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${instance}" "${dx}" "${candidate_odb}" "${variant}" \
    "${ROOT_DIR}/build/power/place_spef_calibration_${TAG}_${key}" \
    "${routed_acceptance}" "${comparison_status}" >> "${BATCH_DIR}/batch_manifest.tsv"
  echo "Completed ${key}; routed acceptance=${routed_acceptance}."
done

AFTER_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_place_odb_sha256_before\t%s\nbaseline_place_odb_sha256_after\t%s\n' \
  "${BASELINE_SHA}" "${AFTER_SHA}" > "${BATCH_DIR}/integrity.tsv"
if [[ "${BASELINE_SHA}" != "${AFTER_SHA}" ]]; then
  echo "Baseline placement ODB changed during calibration batch." >&2
  exit 3
fi
python3 "${ROOT_DIR}/scripts/summarize_place_spef_calibration_batch.py" \
  --batch-dir "${BATCH_DIR}" \
  --output "${BATCH_DIR}/summary.txt"
echo "Batch complete: ${BATCH_DIR}"
echo "Baseline placement ODB SHA-256 unchanged: ${AFTER_SHA}"
