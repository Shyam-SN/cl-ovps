#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW_DIR="${ROOT_DIR}/tools/openroad-flow-scripts/flow"
DESIGN_CONFIG="${ROOT_DIR}/designs/picorv32/flow/config.mk"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASELINE_RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
SOURCE_PLACE_ODB="${BASELINE_RESULT_DIR}/3_5_place_dp.odb"
SOURCE_PLACE_SDC="${BASELINE_RESULT_DIR}/3_place.sdc"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${SOURCE_PLACE_ODB}" || ! -s "${SOURCE_PLACE_SDC}" ]]; then
  echo "Missing OpenROAD build or baseline pre-CTS placement checkpoint." >&2
  exit 1
fi

TAG="${NULL_REPEAT_TAG:-$(date +%Y%m%d_%H%M%S)}"
FLOW_A="null_repeat_${TAG}_a"
FLOW_B="null_repeat_${TAG}_b"
REPORT_DIR="${ROOT_DIR}/build/power/null_repeat_${TAG}"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/null_repeat_${TAG}"
RESULT_ROOT="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline"
for path in \
  "${RESULT_ROOT}/${FLOW_A}" "${RESULT_ROOT}/${FLOW_B}" \
  "${REPORT_DIR}" "${ACTIVITY_DIR}"; do
  if [[ -e "${path}" ]]; then
    echo "Repeatability output already exists: ${path}; choose a fresh NULL_REPEAT_TAG." >&2
    exit 1
  fi
done

mkdir -p "${REPORT_DIR}/run_a" "${REPORT_DIR}/run_b" \
  "${ACTIVITY_DIR}/run_a" "${ACTIVITY_DIR}/run_b" \
  "${REPORT_DIR}/net_attribution/run_a" "${REPORT_DIR}/net_attribution/run_b"

YOSYS_EXE="${YOSYS_EXE:-$(command -v yosys)}"
PYTHON_EXE="${PYTHON_EXE:-$(command -v python3)}"
NUM_CORES="${NUM_CORES:-4}"
MAX_IDLE_VARIATION_NW="${MAX_IDLE_VARIATION_NW:-4.5}"

source_odb_sha="$(shasum -a 256 "${SOURCE_PLACE_ODB}" | awk '{print $1}')"
source_sdc_sha="$(shasum -a 256 "${SOURCE_PLACE_SDC}" | awk '{print $1}')"
baseline_final_odb_sha="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
baseline_final_spef_sha="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
{
  printf 'source_place_odb\t%s\n' "${source_odb_sha}"
  printf 'source_place_sdc\t%s\n' "${source_sdc_sha}"
  printf 'baseline_final_odb_before\t%s\n' "${baseline_final_odb_sha}"
  printf 'baseline_final_spef_before\t%s\n' "${baseline_final_spef_sha}"
  printf 'num_cores\t%s\n' "${NUM_CORES}"
} > "${REPORT_DIR}/integrity.tsv"
{
  printf 'source_place_odb\t%s\n' "${source_odb_sha}"
  printf 'source_place_sdc\t%s\n' "${source_sdc_sha}"
  printf 'num_cores\t%s\n' "${NUM_CORES}"
  printf 'max_idle_variation_nw\t%s\n' "${MAX_IDLE_VARIATION_NW}"
} > "${REPORT_DIR}/manifest.tsv"

run_route() {
  local flow_variant="$1"
  local run_name="$2"
  local result_dir="${RESULT_ROOT}/${flow_variant}"
  local run_report_dir="${REPORT_DIR}/${run_name}"
  mkdir -p "${result_dir}"
  # Both trials begin from byte-identical post-detailed-placement databases
  # and the same SDC; all CTS/routing/RCX products are isolated by variant.
  cp "${SOURCE_PLACE_ODB}" "${result_dir}/3_5_place_dp.odb"
  cp "${SOURCE_PLACE_ODB}" "${result_dir}/3_place.odb"
  cp "${SOURCE_PLACE_SDC}" "${result_dir}/3_place.sdc"
  for checkpoint in 3_5_place_dp.odb 3_place.odb; do
    local copied_sha
    copied_sha="$(shasum -a 256 "${result_dir}/${checkpoint}" | awk '{print $1}')"
    if [[ "${copied_sha}" != "${source_odb_sha}" ]]; then
      echo "Copied placement checkpoint does not match source: ${flow_variant}/${checkpoint}" >&2
      exit 2
    fi
  done
  local copied_sdc_sha
  copied_sdc_sha="$(shasum -a 256 "${result_dir}/3_place.sdc" | awk '{print $1}')"
  if [[ "${copied_sdc_sha}" != "${source_sdc_sha}" ]]; then
    echo "Copied SDC does not match source: ${flow_variant}" >&2
    exit 2
  fi
  printf '%s\t%s\n%s\t%s\n' \
    "${run_name}_input_odb_sha256" "${source_odb_sha}" \
    "${run_name}_input_sdc_sha256" "${source_sdc_sha}" >> "${REPORT_DIR}/manifest.tsv"

  echo "Routing null-move trial ${run_name} (${flow_variant}) from identical placement input."
  make -C "${FLOW_DIR}" \
    DESIGN_CONFIG="${DESIGN_CONFIG}" \
    DESIGN_NICKNAME=picorv32_baseline \
    FLOW_VARIANT="${flow_variant}" \
    WORK_HOME="${ROOT_DIR}/build/orfs" \
    OPENROAD_EXE="${OPENROAD_EXE}" \
    YOSYS_EXE="${YOSYS_EXE}" \
    PYTHON_EXE="${PYTHON_EXE}" \
    NUM_CORES="${NUM_CORES}" \
    do-cts do-route do-finish \
    > "${run_report_dir}/orfs.log" 2>&1

  local result_file
  for result_file in 6_final.odb 6_final.sdc 6_final.spef 6_final.v; do
    if [[ ! -s "${result_dir}/${result_file}" ]]; then
      echo "ORFS did not produce ${flow_variant}/${result_file}." >&2
      exit 3
    fi
  done
  printf '%s\n' "${flow_variant}" > "${run_report_dir}/route_complete.txt"
}

run_route "${FLOW_A}" run_a
run_route "${FLOW_B}" run_b

for pair in "run_a:${FLOW_A}" "run_b:${FLOW_B}"; do
  run_name="${pair%%:*}"
  flow_variant="${pair#*:}"
  result_dir="${RESULT_ROOT}/${flow_variant}"
  run_report_dir="${REPORT_DIR}/${run_name}"
  GATELEVEL_NETLIST="${result_dir}/6_final.v" \
    ACTIVITY_OUT_DIR="${ACTIVITY_DIR}/${run_name}" \
    "${ROOT_DIR}/designs/picorv32/sim/run_activity.sh" \
    > "${run_report_dir}/simulation.log" 2>&1
  for workload in idle typical peak; do
    SPEF_ODB="${result_dir}/6_final.odb" \
      SPEF_SDC="${result_dir}/6_final.sdc" \
      SPEF_FILE="${result_dir}/6_final.spef" \
      ACTIVITY_VCD="${ACTIVITY_DIR}/${run_name}/${workload}.vcd" \
      WORKLOAD="${workload}" \
      ACTIVITY_TRACE=1 \
      CAP_REPORT="${REPORT_DIR}/net_attribution/${run_name}/${workload}.driver_caps.tsv" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_spef_power.tcl" \
      > "${run_report_dir}/${workload}.log" 2>&1
    cp "${run_report_dir}/${workload}.log" \
      "${REPORT_DIR}/net_attribution/${run_name}/${workload}.log"
  done
done

baseline_final_odb_after="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.odb" | awk '{print $1}')"
baseline_final_spef_after="$(shasum -a 256 "${BASELINE_RESULT_DIR}/6_final.spef" | awk '{print $1}')"
printf 'baseline_final_odb_after\t%s\nbaseline_final_spef_after\t%s\n' \
  "${baseline_final_odb_after}" "${baseline_final_spef_after}" >> "${REPORT_DIR}/integrity.tsv"
if [[ "${baseline_final_odb_sha}" != "${baseline_final_odb_after}" || \
      "${baseline_final_spef_sha}" != "${baseline_final_spef_after}" ]]; then
  echo "The original baseline routed ODB or SPEF changed during the repeatability run." >&2
  exit 4
fi

for run_pair in "${FLOW_A}:run_a" "${FLOW_B}:run_b"; do
  flow_variant="${run_pair%%:*}"
  run_name="${run_pair#*:}"
  result_dir="${RESULT_ROOT}/${flow_variant}"
  {
    printf '%s\t%s\n' "${run_name}_final_odb_sha256" "$(shasum -a 256 "${result_dir}/6_final.odb" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_sdc_sha256" "$(shasum -a 256 "${result_dir}/6_final.sdc" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_spef_sha256" "$(shasum -a 256 "${result_dir}/6_final.spef" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_netlist_sha256" "$(shasum -a 256 "${result_dir}/6_final.v" | awk '{print $1}')"
  } >> "${REPORT_DIR}/integrity.tsv"
  {
    printf '%s\t%s\n' "${run_name}_final_odb_sha256" "$(shasum -a 256 "${result_dir}/6_final.odb" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_sdc_sha256" "$(shasum -a 256 "${result_dir}/6_final.sdc" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_spef_sha256" "$(shasum -a 256 "${result_dir}/6_final.spef" | awk '{print $1}')"
    printf '%s\t%s\n' "${run_name}_final_netlist_sha256" "$(shasum -a 256 "${result_dir}/6_final.v" | awk '{print $1}')"
  } >> "${REPORT_DIR}/manifest.tsv"
done

python3 "${ROOT_DIR}/scripts/summarize_spef_net_attribution.py" \
  --baseline-dir "${REPORT_DIR}/net_attribution/run_a" \
  --candidate-dir "${REPORT_DIR}/net_attribution/run_b" \
  --output-dir "${REPORT_DIR}/net_attribution" \
  --summary "${REPORT_DIR}/net_attribution/summary.txt" \
  > "${REPORT_DIR}/net_attribution/summarizer.log"

python3 "${ROOT_DIR}/scripts/summarize_null_repeatability.py" \
  --run-a-dir "${REPORT_DIR}/run_a" \
  --run-b-dir "${REPORT_DIR}/run_b" \
  --manifest "${REPORT_DIR}/manifest.tsv" \
  --run-a-spef "${RESULT_ROOT}/${FLOW_A}/6_final.spef" \
  --run-b-spef "${RESULT_ROOT}/${FLOW_B}/6_final.spef" \
  --max-idle-variation-nw "${MAX_IDLE_VARIATION_NW}" \
  --output "${REPORT_DIR}/summary.txt"

echo "Null-move repeatability report: ${REPORT_DIR}/summary.txt"
echo "Per-net comparison: ${REPORT_DIR}/net_attribution/summary.txt"
echo "Original baseline routed ODB/SPEF hashes are unchanged."
