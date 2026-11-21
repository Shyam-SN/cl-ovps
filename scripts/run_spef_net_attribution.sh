#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
DEFAULT_REPORT_DIR="${ROOT_DIR}/build/power/one_move_spef_20261026_222307"
FLOW_VARIANT="${SPEF_FLOW_VARIANT:-$(cat "${DEFAULT_REPORT_DIR}/flow_variant.txt")}" 
REPORT_DIR="${ROOT_DIR}/build/power/${FLOW_VARIANT}"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/${FLOW_VARIANT}"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
CANDIDATE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/${FLOW_VARIANT}"
ANALYSIS_DIR="${REPORT_DIR}/net_attribution"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${REPORT_DIR}/route_complete.txt" ]]; then
  echo "Missing OpenROAD build or completed routed SPEF run for ${FLOW_VARIANT}." >&2
  exit 1
fi

for design in baseline candidate; do
  if [[ "${design}" == baseline ]]; then
    RESULT_DIR="${BASELINE_RESULT_DIR}"
  else
    RESULT_DIR="${CANDIDATE_RESULT_DIR}"
  fi
  for artifact in 6_final.odb 6_final.sdc 6_final.spef; do
    if [[ ! -s "${RESULT_DIR}/${artifact}" ]]; then
      echo "Missing routed artifact: ${RESULT_DIR}/${artifact}" >&2
      exit 1
    fi
  done
  for workload in idle typical peak; do
    if [[ ! -s "${ACTIVITY_DIR}/${design}/${workload}.vcd" ]]; then
      echo "Missing matched ${design}/${workload} VCD: ${ACTIVITY_DIR}/${design}/${workload}.vcd" >&2
      exit 1
    fi
  done
done

mkdir -p "${ANALYSIS_DIR}/baseline" "${ANALYSIS_DIR}/candidate"
BASELINE_ODB_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
BASELINE_SPEF_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
printf 'baseline_odb_before\t%s\nbaseline_spef_before\t%s\n' \
  "${BASELINE_ODB_SHA}" "${BASELINE_SPEF_SHA}" > "${ANALYSIS_DIR}/baseline_integrity.tsv"

for design in baseline candidate; do
  if [[ "${design}" == baseline ]]; then
    RESULT_DIR="${BASELINE_RESULT_DIR}"
  else
    RESULT_DIR="${CANDIDATE_RESULT_DIR}"
  fi
  for workload in idle typical peak; do
    SPEF_ODB="${RESULT_DIR}/6_final.odb" \
      SPEF_SDC="${RESULT_DIR}/6_final.sdc" \
      SPEF_FILE="${RESULT_DIR}/6_final.spef" \
      ACTIVITY_VCD="${ACTIVITY_DIR}/${design}/${workload}.vcd" \
      WORKLOAD="${workload}" \
      ACTIVITY_TRACE=1 \
      CAP_REPORT="${ANALYSIS_DIR}/${design}/${workload}.driver_caps.tsv" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_spef_power.tcl" \
      > "${ANALYSIS_DIR}/${design}/${workload}.log" 2>&1
  done
done

set +e
python3 "${ROOT_DIR}/scripts/summarize_spef_net_attribution.py" \
  --baseline-dir "${ANALYSIS_DIR}/baseline" \
  --candidate-dir "${ANALYSIS_DIR}/candidate" \
  --output-dir "${ANALYSIS_DIR}" \
  --summary "${ANALYSIS_DIR}/summary.txt"
SUMMARY_STATUS=$?
set -e

AFTER_BASELINE_ODB_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
AFTER_BASELINE_SPEF_SHA="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
printf 'baseline_odb_after\t%s\nbaseline_spef_after\t%s\n' \
  "${AFTER_BASELINE_ODB_SHA}" "${AFTER_BASELINE_SPEF_SHA}" >> "${ANALYSIS_DIR}/baseline_integrity.tsv"
if [[ "${BASELINE_ODB_SHA}" != "${AFTER_BASELINE_ODB_SHA}" || "${BASELINE_SPEF_SHA}" != "${AFTER_BASELINE_SPEF_SHA}" ]]; then
  echo "Baseline routed ODB or SPEF changed during read-only attribution." >&2
  exit 3
fi

echo "Baseline routed ODB and SPEF hashes are unchanged."
echo "Per-net attribution written to ${ANALYSIS_DIR}"
exit "${SUMMARY_STATUS}"
