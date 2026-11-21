#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW_DIR="${ROOT_DIR}/tools/openroad-flow-scripts/flow"
DESIGN_CONFIG="${ROOT_DIR}/designs/gcd/flow/config.mk"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
BASELINE_ACTIVITY_DIR="${ROOT_DIR}/build/activity/gcd_baseline20ns/routed"
MOVE_CANDIDATE_ODB="${MOVE_CANDIDATE_ODB:-}"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${MOVE_CANDIDATE_ODB}" ]]; then
  echo "Set MOVE_CANDIDATE_ODB to a saved GCD placement candidate ODB." >&2
  exit 1
fi

FLOW_VARIANT="${CANDIDATE_FLOW_VARIANT:-gcd_candidate_$(date +%Y%m%d_%H%M%S)}"
RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/gcd_power/${FLOW_VARIANT}"
REPORT_DIR="${ROOT_DIR}/build/power/${FLOW_VARIANT}"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/${FLOW_VARIANT}"
if [[ -e "${RESULT_DIR}" || -e "${REPORT_DIR}" || -e "${ACTIVITY_DIR}" ]]; then
  echo "Candidate run path already exists for FLOW_VARIANT=${FLOW_VARIANT}; choose a fresh variant." >&2
  exit 1
fi
for artifact in 3_place.sdc 6_final.odb 6_final.sdc 6_final.spef 6_final.v; do
  if [[ ! -s "${BASELINE_RESULT_DIR}/${artifact}" ]]; then
    echo "Missing GCD baseline artifact: ${BASELINE_RESULT_DIR}/${artifact}" >&2
    exit 1
  fi
done

mkdir -p "${RESULT_DIR}" "${REPORT_DIR}/baseline" "${REPORT_DIR}/candidate" \
  "${ACTIVITY_DIR}/candidate"
cp "${MOVE_CANDIDATE_ODB}" "${RESULT_DIR}/3_5_place_dp.odb"
cp "${MOVE_CANDIDATE_ODB}" "${RESULT_DIR}/3_place.odb"
cp "${BASELINE_RESULT_DIR}/3_place.sdc" "${RESULT_DIR}/3_place.sdc"

BASELINE_ODB_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
BASELINE_SPEF_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
CANDIDATE_INPUT_SHA="$(shasum -a 256 "${MOVE_CANDIDATE_ODB}" | awk '{print $1}')"
printf 'baseline_odb_before\t%s\nbaseline_spef_before\t%s\ncandidate_place_odb_before\t%s\n' \
  "${BASELINE_ODB_SHA}" "${BASELINE_SPEF_SHA}" "${CANDIDATE_INPUT_SHA}" \
  > "${REPORT_DIR}/integrity.tsv"

echo "Routing GCD candidate into FLOW_VARIANT=${FLOW_VARIANT}"
make -C "${FLOW_DIR}" \
  DESIGN_CONFIG="${DESIGN_CONFIG}" \
  DESIGN_NICKNAME=gcd_power \
  FLOW_VARIANT="${FLOW_VARIANT}" \
  WORK_HOME="${ROOT_DIR}/build/orfs" \
  OPENROAD_EXE="${OPENROAD_EXE}" \
  YOSYS_EXE="${YOSYS_EXE:-$(command -v yosys)}" \
  PYTHON_EXE="${PYTHON_EXE:-$(command -v python3)}" \
  NUM_CORES="${NUM_CORES:-4}" \
  do-cts do-route do-finish > "${REPORT_DIR}/orfs.log" 2>&1

FINAL_ODB="${RESULT_DIR}/6_final.odb"
FINAL_SDC="${RESULT_DIR}/6_final.sdc"
FINAL_SPEF="${RESULT_DIR}/6_final.spef"
FINAL_NETLIST="${RESULT_DIR}/6_final.v"
for artifact in "${FINAL_ODB}" "${FINAL_SDC}" "${FINAL_SPEF}" "${FINAL_NETLIST}"; do
  if [[ ! -s "${artifact}" ]]; then
    echo "ORFS did not produce required routed artifact: ${artifact}" >&2
    exit 2
  fi
done
printf '%s\n' "${FLOW_VARIANT}" > "${REPORT_DIR}/route_complete.txt"

GATELEVEL_NETLIST="${FINAL_NETLIST}" ACTIVITY_OUT_DIR="${ACTIVITY_DIR}/candidate" \
  "${ROOT_DIR}/designs/gcd/sim/run_activity.sh" > "${REPORT_DIR}/candidate/simulation.log" 2>&1

for workload in idle typical peak; do
  SPEF_ODB="${BASELINE_RESULT_DIR}/6_final.odb" \
  SPEF_SDC="${BASELINE_RESULT_DIR}/6_final.sdc" \
  SPEF_FILE="${BASELINE_RESULT_DIR}/6_final.spef" \
  ACTIVITY_VCD="${BASELINE_ACTIVITY_DIR}/${workload}.vcd" \
  WORKLOAD="${workload}" ACTIVITY_TRACE=1 \
  CAP_REPORT="${REPORT_DIR}/baseline/${workload}.driver_caps.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/gcd_report_spef_power.tcl" \
    > "${REPORT_DIR}/baseline/${workload}.log" 2>&1

  SPEF_ODB="${FINAL_ODB}" SPEF_SDC="${FINAL_SDC}" SPEF_FILE="${FINAL_SPEF}" \
  ACTIVITY_VCD="${ACTIVITY_DIR}/candidate/${workload}.vcd" \
  WORKLOAD="${workload}" ACTIVITY_TRACE=1 \
  CAP_REPORT="${REPORT_DIR}/candidate/${workload}.driver_caps.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/gcd_report_spef_power.tcl" \
    > "${REPORT_DIR}/candidate/${workload}.log" 2>&1
done

set +e
python3 "${ROOT_DIR}/scripts/summarize_spef_comparison.py" \
  --baseline-dir "${REPORT_DIR}/baseline" \
  --candidate-dir "${REPORT_DIR}/candidate" \
  --route-marker "${REPORT_DIR}/route_complete.txt" \
  --output "${REPORT_DIR}/summary.txt"
SUMMARY_STATUS=$?
set -e

AFTER_BASELINE_ODB_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
AFTER_BASELINE_SPEF_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
AFTER_CANDIDATE_INPUT_SHA="$(shasum -a 256 "${MOVE_CANDIDATE_ODB}" | awk '{print $1}')"
printf 'baseline_odb_after\t%s\nbaseline_spef_after\t%s\ncandidate_place_odb_after\t%s\n' \
  "${AFTER_BASELINE_ODB_SHA}" "${AFTER_BASELINE_SPEF_SHA}" "${AFTER_CANDIDATE_INPUT_SHA}" \
  >> "${REPORT_DIR}/integrity.tsv"
if [[ "${BASELINE_ODB_SHA}" != "${AFTER_BASELINE_ODB_SHA}" \
   || "${BASELINE_SPEF_SHA}" != "${AFTER_BASELINE_SPEF_SHA}" \
   || "${CANDIDATE_INPUT_SHA}" != "${AFTER_CANDIDATE_INPUT_SHA}" ]]; then
  echo "A baseline or source placement artifact changed during candidate validation." >&2
  exit 3
fi
echo "Baseline routed ODB/SPEF and candidate source ODB hashes are unchanged."
echo "SPEF comparison written to ${REPORT_DIR}/summary.txt"
# A completed route is a successful experiment even when the power gate
# rejects it. The summary records that decision for the caller.
if [[ "${SUMMARY_STATUS}" -gt 2 ]]; then exit "${SUMMARY_STATUS}"; fi
