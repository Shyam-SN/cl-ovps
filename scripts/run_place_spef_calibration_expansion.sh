#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
MOVE_EVAL_DIR="${ROOT_DIR}/build/power/move_eval"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
SEED_BATCH="${ROOT_DIR}/build/power/place_spef_batch_20261027_002801"
SELECTION_TSV="${PLACE_SPEF_EXPANSION_SELECTION:-${ROOT_DIR}/build/power/place_spef_expansion_selection.tsv}"
TAG="${PLACE_SPEF_EXPANSION_TAG:-$(date +%Y%m%d_%H%M%S)}"
BATCH_DIR="${ROOT_DIR}/build/power/place_spef_expansion_${TAG}"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" ]]; then
  echo "Missing OpenROAD build or detailed-placement baseline." >&2
  exit 1
fi
if [[ ! -s "${SELECTION_TSV}" || ! -s "${SEED_BATCH}/batch_manifest.tsv" ]]; then
  echo "Missing selected move TSV or seed calibration batch." >&2
  exit 1
fi
if [[ -e "${BATCH_DIR}" ]]; then
  echo "Expansion output already exists: ${BATCH_DIR}; choose a fresh PLACE_SPEF_EXPANSION_TAG." >&2
  exit 1
fi

mkdir -p "${BATCH_DIR}"
cp "${SEED_BATCH}/batch_manifest.tsv" "${BATCH_DIR}/batch_manifest.tsv"
BASELINE_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_place_odb_sha256_before\t%s\n' "${BASELINE_SHA}" > "${BATCH_DIR}/integrity.tsv"
printf 'instance\tdx_dbu\tstratum\tplacement_odb\tflow_variant\tcalibration_report\tpredicted_typical_delta_w\n' \
  > "${BATCH_DIR}/selected_moves.tsv"

seen_moves=$'\n'
move_count=0
while IFS=$'\t' read -r instance dx stratum; do
  [[ -z "${instance}" || "${instance}" == "instance" ]] && continue
  if [[ ! "${dx}" =~ ^-?[0-9]+$ || -z "${stratum}" ]]; then
    echo "Malformed selection row: ${instance} ${dx} ${stratum}" >&2
    exit 1
  fi
  key="${instance}_${dx}"
  if [[ "${seen_moves}" == *$'\n'"${key}"$'\n'* ]]; then
    echo "Duplicate selected move: ${instance} dx=${dx}" >&2
    exit 1
  fi
  seen_moves+="${key}"$'\n'
  move_count=$((move_count + 1))
  if [[ "${dx}" == -* ]]; then direction="m${dx#-}"; else direction="p${dx}"; fi
  candidate_key="${instance}_${direction}"
  candidate_dir="${BATCH_DIR}/${candidate_key}"
  candidate_odb="${candidate_dir}/3_5_place_dp.odb"
  move_summary="${candidate_dir}/applied_move.tsv"
  variant="placecal_${TAG}_${candidate_key}"
  mkdir -p "${candidate_dir}"

  echo "Preparing ${move_count}: ${instance} dx=${dx} DBU (${stratum}) from the unchanged baseline."
  ACTIVITY_VCD="${ROOT_DIR}/build/activity/detailed_place/typical.vcd" \
    MOVE_EVAL_DIR="${MOVE_EVAL_DIR}" \
    MOVE_SELECTED_INSTANCE="${instance}" MOVE_SELECTED_DX="${dx}" \
    CANDIDATE_ODB="${candidate_odb}" MOVE_APPLY_SUMMARY="${move_summary}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/apply_calibration_move.tcl" \
    > "${candidate_dir}/apply.log" 2>&1
  candidate_sha="$(shasum -a 256 "${candidate_odb}" | awk '{print $1}')"

  comparison_status=0
  MOVE_CANDIDATE_ODB="${candidate_odb}" CANDIDATE_FLOW_VARIANT="${variant}" \
    "${ROOT_DIR}/scripts/run_candidate_spef_flow.sh" \
    > "${candidate_dir}/route_driver.log" 2>&1 || comparison_status=$?
  if [[ ! -s "${ROOT_DIR}/build/power/${variant}/route_complete.txt" ]]; then
    echo "Routing did not complete for ${candidate_key}; see ${candidate_dir}/route_driver.log" >&2
    exit 2
  fi

  SPEF_FLOW_VARIANT="${variant}" "${ROOT_DIR}/scripts/run_spef_net_attribution.sh" \
    > "${candidate_dir}/attribution_driver.log" 2>&1
  CANDIDATE_ODB="${candidate_odb}" \
    ROUTED_REPORT_DIR="${ROOT_DIR}/build/power/${variant}" \
    MOVE_APPLY_SUMMARY="${move_summary}" \
    PLACE_SPEF_CAL_TAG="${TAG}_${candidate_key}" \
    "${ROOT_DIR}/scripts/run_place_spef_calibration.sh" \
    > "${candidate_dir}/placement_calibration_driver.log" 2>&1

  candidate_after="$(shasum -a 256 "${candidate_odb}" | awk '{print $1}')"
  if [[ "${candidate_sha}" != "${candidate_after}" ]]; then
    echo "Candidate placement ODB changed during calibration: ${candidate_key}" >&2
    exit 3
  fi
  routed_acceptance="unknown"
  route_summary="${ROOT_DIR}/build/power/${variant}/summary.txt"
  if grep -q 'SPEF candidate acceptance: PASS' "${route_summary}"; then
    routed_acceptance="PASS"
  elif grep -q 'SPEF candidate acceptance: REJECTED' "${route_summary}"; then
    routed_acceptance="REJECTED"
  fi
  predicted_typical="$(awk -F '\t' 'NR == 2 {print $8}' "${move_summary}")"
  calibration_report="${ROOT_DIR}/build/power/place_spef_calibration_${TAG}_${candidate_key}"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${instance}" "${dx}" "${candidate_odb}" "${variant}" "${calibration_report}" \
    "${routed_acceptance}" "${comparison_status}" >> "${BATCH_DIR}/batch_manifest.tsv"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${instance}" "${dx}" "${stratum}" "${candidate_odb}" "${variant}" \
    "${calibration_report}" "${predicted_typical}" >> "${BATCH_DIR}/selected_moves.tsv"
  echo "Completed ${candidate_key}; routed acceptance=${routed_acceptance}; placement ODB hash unchanged."
done < "${SELECTION_TSV}"

if [[ "${move_count}" -lt 1 ]]; then
  echo "Selection TSV contained no moves." >&2
  exit 1
fi

AFTER_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_place_odb_sha256_after\t%s\n' "${AFTER_SHA}" >> "${BATCH_DIR}/integrity.tsv"
if [[ "${BASELINE_SHA}" != "${AFTER_SHA}" ]]; then
  echo "Baseline placement ODB changed during calibration expansion." >&2
  exit 4
fi

python3 "${ROOT_DIR}/scripts/summarize_place_spef_calibration_batch.py" \
  --batch-dir "${BATCH_DIR}" \
  --output "${BATCH_DIR}/summary.txt"
echo "Expansion complete: ${BATCH_DIR}"
echo "Added ${move_count} routed calibration moves; baseline placement ODB SHA-256 unchanged: ${AFTER_SHA}"
