#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
BASELINE_ODB="${BASELINE_RESULT_DIR}/3_5_place_dp.odb"
PLACEMENT_SDC="${BASELINE_RESULT_DIR}/3_place.sdc"
CANDIDATE_ODB="${CANDIDATE_ODB:-${ROOT_DIR}/build/power/one_move_candidate/3_5_place_dp_one_move.odb}"
ROUTED_REPORT_DIR="${ROUTED_REPORT_DIR:-${ROOT_DIR}/build/power/one_move_spef_20261026_222307}"
ROUTED_ATTR_DIR="${ROUTED_REPORT_DIR}/net_attribution"
ROUTED_VARIANT="$(cat "${ROUTED_REPORT_DIR}/flow_variant.txt")"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${BASELINE_ODB}" || ! -s "${PLACEMENT_SDC}" || \
      ! -s "${CANDIDATE_ODB}" || ! -s "${ROUTED_REPORT_DIR}/route_complete.txt" ]]; then
  echo "Missing OpenROAD, placement candidate, or completed routed comparison." >&2
  exit 1
fi
for design in baseline candidate; do
  for workload in idle typical peak; do
    if [[ ! -s "${ROUTED_ATTR_DIR}/${design}/${workload}.driver_caps.tsv" || \
          ! -s "${ROUTED_ATTR_DIR}/${design}/${workload}.log" ]]; then
      echo "Missing routed ${design}/${workload} net-attribution inputs." >&2
      exit 1
    fi
  done
done

TAG="${PLACE_SPEF_CAL_TAG:-$(date +%Y%m%d_%H%M%S)}"
REPORT_DIR="${ROOT_DIR}/build/power/place_spef_calibration_${TAG}"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/place_spef_calibration_${TAG}"
if [[ -e "${REPORT_DIR}" || -e "${ACTIVITY_DIR}" ]]; then
  echo "Calibration output already exists; choose a fresh PLACE_SPEF_CAL_TAG." >&2
  exit 1
fi
mkdir -p "${REPORT_DIR}/placement/baseline" "${REPORT_DIR}/placement/candidate" \
  "${ACTIVITY_DIR}/baseline" "${ACTIVITY_DIR}/candidate"

BASELINE_SHA="$(shasum -a 256 "${BASELINE_ODB}" | awk '{print $1}')"
CANDIDATE_SHA="$(shasum -a 256 "${CANDIDATE_ODB}" | awk '{print $1}')"
SDC_SHA="$(shasum -a 256 "${PLACEMENT_SDC}" | awk '{print $1}')"
ROUTED_BASELINE_SHA="$(shasum -a 256 "${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline/6_final.spef" | awk '{print $1}')"
ROUTED_CANDIDATE_SHA="$(shasum -a 256 "${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/${ROUTED_VARIANT}/6_final.spef" | awk '{print $1}')"
{
  printf 'baseline_place_odb_sha256\t%s\n' "${BASELINE_SHA}"
  printf 'candidate_place_odb_sha256\t%s\n' "${CANDIDATE_SHA}"
  printf 'placement_sdc_sha256\t%s\n' "${SDC_SHA}"
  printf 'routed_flow_variant\t%s\n' "${ROUTED_VARIANT}"
  printf 'routed_baseline_spef_sha256_before\t%s\n' "${ROUTED_BASELINE_SHA}"
  printf 'routed_candidate_spef_sha256_before\t%s\n' "${ROUTED_CANDIDATE_SHA}"
} > "${REPORT_DIR}/manifest.tsv"

for design in baseline candidate; do
  if [[ "${design}" == baseline ]]; then
    INPUT_ODB="${BASELINE_ODB}"
  else
    INPUT_ODB="${CANDIDATE_ODB}"
  fi
  PLACE_NETLIST="${REPORT_DIR}/placement/${design}/placement.v"
  PLACEMENT_ODB="${INPUT_ODB}" PLACEMENT_NETLIST="${PLACE_NETLIST}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_place_netlist.tcl" \
    > "${REPORT_DIR}/placement/${design}/export.log" 2>&1
  GATELEVEL_NETLIST="${PLACE_NETLIST}" ACTIVITY_OUT_DIR="${ACTIVITY_DIR}/${design}" \
    "${ROOT_DIR}/designs/picorv32/sim/run_activity.sh" \
    > "${REPORT_DIR}/placement/${design}/simulation.log" 2>&1

  for workload in idle typical peak; do
    ACTIVITY_VCD="${ACTIVITY_DIR}/${design}/${workload}.vcd" \
      ACTIVITY_TRACE=1 \
      CAP_REPORT="${REPORT_DIR}/placement/${design}/${workload}.driver_caps.tsv" \
      PLACEMENT_ODB="${INPUT_ODB}" \
      PLACEMENT_SDC="${PLACEMENT_SDC}" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_place_power.tcl" \
      > "${REPORT_DIR}/placement/${design}/${workload}.log" 2>&1
  done
done

BASELINE_AFTER="$(shasum -a 256 "${BASELINE_ODB}" | awk '{print $1}')"
CANDIDATE_AFTER="$(shasum -a 256 "${CANDIDATE_ODB}" | awk '{print $1}')"
SDC_AFTER="$(shasum -a 256 "${PLACEMENT_SDC}" | awk '{print $1}')"
ROUTED_BASELINE_AFTER="$(shasum -a 256 "${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline/6_final.spef" | awk '{print $1}')"
ROUTED_CANDIDATE_AFTER="$(shasum -a 256 "${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/${ROUTED_VARIANT}/6_final.spef" | awk '{print $1}')"
{
  printf 'baseline_place_odb_sha256_after\t%s\n' "${BASELINE_AFTER}"
  printf 'candidate_place_odb_sha256_after\t%s\n' "${CANDIDATE_AFTER}"
  printf 'placement_sdc_sha256_after\t%s\n' "${SDC_AFTER}"
  printf 'routed_baseline_spef_sha256_after\t%s\n' "${ROUTED_BASELINE_AFTER}"
  printf 'routed_candidate_spef_sha256_after\t%s\n' "${ROUTED_CANDIDATE_AFTER}"
} >> "${REPORT_DIR}/manifest.tsv"
if [[ "${BASELINE_SHA}" != "${BASELINE_AFTER}" || \
      "${CANDIDATE_SHA}" != "${CANDIDATE_AFTER}" || \
      "${SDC_SHA}" != "${SDC_AFTER}" || \
      "${ROUTED_BASELINE_SHA}" != "${ROUTED_BASELINE_AFTER}" || \
      "${ROUTED_CANDIDATE_SHA}" != "${ROUTED_CANDIDATE_AFTER}" ]]; then
  echo "A source placement or routed SPEF artifact changed during read-only calibration." >&2
  exit 2
fi

python3 "${ROOT_DIR}/scripts/summarize_place_spef_calibration.py" \
  --placement-baseline-dir "${REPORT_DIR}/placement/baseline" \
  --placement-candidate-dir "${REPORT_DIR}/placement/candidate" \
  --routed-attribution-dir "${ROUTED_ATTR_DIR}" \
  --move-summary "${MOVE_APPLY_SUMMARY:-${ROOT_DIR}/build/power/one_move_candidate/applied_move.tsv}" \
  --move-eval-table "${ROOT_DIR}/build/power/move_eval/candidate_moves_typical.tsv" \
  --manifest "${REPORT_DIR}/manifest.tsv" \
  --output-dir "${REPORT_DIR}"

echo "Placement-to-SPEF calibration written to ${REPORT_DIR}/summary.txt"
