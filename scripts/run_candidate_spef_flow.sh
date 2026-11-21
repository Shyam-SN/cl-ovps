#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW_DIR="${ROOT_DIR}/tools/openroad-flow-scripts/flow"
DESIGN_CONFIG="${ROOT_DIR}/designs/picorv32/flow/config.mk"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
MOVE_CANDIDATE_ODB="${MOVE_CANDIDATE_ODB:-${ROOT_DIR}/build/power/one_move_candidate/3_5_place_dp_one_move.odb}"

if [[ ! -x "${OPENROAD_EXE}" || ! -f "${MOVE_CANDIDATE_ODB}" ]]; then
  echo "Missing OpenROAD build or verified one-move candidate. Run ./scripts/run_best_move_candidate.sh first." >&2
  exit 1
fi

FLOW_VARIANT="${CANDIDATE_FLOW_VARIANT:-one_move_spef_$(date +%Y%m%d_%H%M%S)}"
RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/${FLOW_VARIANT}"
REPORT_DIR="${ROOT_DIR}/build/power/${FLOW_VARIANT}"
ACTIVITY_BASE="${ROOT_DIR}/build/activity/${FLOW_VARIANT}"
if [[ -e "${RESULT_DIR}" || -e "${REPORT_DIR}" || -e "${ACTIVITY_BASE}" ]]; then
  echo "Candidate run path already exists for FLOW_VARIANT=${FLOW_VARIANT}; choose a fresh CANDIDATE_FLOW_VARIANT." >&2
  exit 1
fi

mkdir -p "${RESULT_DIR}" "${REPORT_DIR}/baseline" "${REPORT_DIR}/candidate" \
  "${ACTIVITY_BASE}/baseline" "${ACTIVITY_BASE}/candidate"
# Seed the ORFS resume inputs. Generated CTS/routing/final files stay in this
# new FLOW_VARIANT, separate from the baseline run.
cp "${MOVE_CANDIDATE_ODB}" "${RESULT_DIR}/3_5_place_dp.odb"
cp "${MOVE_CANDIDATE_ODB}" "${RESULT_DIR}/3_place.odb"
cp "${BASELINE_RESULT_DIR}/3_place.sdc" "${RESULT_DIR}/3_place.sdc"

YOSYS_EXE="${YOSYS_EXE:-$(command -v yosys)}"
PYTHON_EXE="${PYTHON_EXE:-$(command -v python3)}"
NUM_CORES="${NUM_CORES:-4}"
BASELINE_ODB_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
BASELINE_SPEF_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
printf 'baseline_odb_before\t%s\nbaseline_spef_before\t%s\n' \
  "${BASELINE_ODB_SHA}" "${BASELINE_SPEF_SHA}" > "${REPORT_DIR}/baseline_integrity.tsv"

echo "Routing candidate into FLOW_VARIANT=${FLOW_VARIANT}"
make -C "${FLOW_DIR}" \
  DESIGN_CONFIG="${DESIGN_CONFIG}" \
  DESIGN_NICKNAME=picorv32_baseline \
  FLOW_VARIANT="${FLOW_VARIANT}" \
  WORK_HOME="${ROOT_DIR}/build/orfs" \
  OPENROAD_EXE="${OPENROAD_EXE}" \
  YOSYS_EXE="${YOSYS_EXE}" \
  PYTHON_EXE="${PYTHON_EXE}" \
  NUM_CORES="${NUM_CORES}" \
  do-cts do-route do-finish \
  > "${REPORT_DIR}/orfs.log" 2>&1

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

BASELINE_NETLIST="${BASELINE_RESULT_DIR}/6_final.v"
for design in baseline candidate; do
  if [[ "${design}" == baseline ]]; then
    NETLIST="${BASELINE_NETLIST}"
  else
    NETLIST="${FINAL_NETLIST}"
  fi
  GATELEVEL_NETLIST="${NETLIST}" ACTIVITY_OUT_DIR="${ACTIVITY_BASE}/${design}" \
    "${ROOT_DIR}/designs/picorv32/sim/run_activity.sh" \
    > "${REPORT_DIR}/${design}/simulation.log" 2>&1
done

for workload in idle typical peak; do
  SPEF_ODB="${BASELINE_RESULT_DIR}/6_final.odb" \
    SPEF_SDC="${BASELINE_RESULT_DIR}/6_final.sdc" \
    SPEF_FILE="${BASELINE_RESULT_DIR}/6_final.spef" \
    ACTIVITY_VCD="${ACTIVITY_BASE}/baseline/${workload}.vcd" \
    WORKLOAD="${workload}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_spef_power.tcl" \
    > "${REPORT_DIR}/baseline/${workload}.log" 2>&1

  SPEF_ODB="${FINAL_ODB}" \
    SPEF_SDC="${FINAL_SDC}" \
    SPEF_FILE="${FINAL_SPEF}" \
    ACTIVITY_VCD="${ACTIVITY_BASE}/candidate/${workload}.vcd" \
    WORKLOAD="${workload}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_spef_power.tcl" \
    > "${REPORT_DIR}/candidate/${workload}.log" 2>&1
done

printf '%s\n' "${FLOW_VARIANT}" > "${REPORT_DIR}/flow_variant.txt"
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
printf 'baseline_odb_after\t%s\nbaseline_spef_after\t%s\n' \
  "${AFTER_BASELINE_ODB_SHA}" "${AFTER_BASELINE_SPEF_SHA}" >> "${REPORT_DIR}/baseline_integrity.tsv"
if [[ "${BASELINE_ODB_SHA}" != "${AFTER_BASELINE_ODB_SHA}" || "${BASELINE_SPEF_SHA}" != "${AFTER_BASELINE_SPEF_SHA}" ]]; then
  echo "Baseline routed ODB or SPEF changed during candidate validation." >&2
  exit 3
fi
echo "Baseline routed ODB and SPEF hashes are unchanged."
echo "SPEF comparison written to ${REPORT_DIR}/summary.txt"
exit "${SUMMARY_STATUS}"
