#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW_DIR="${ROOT_DIR}/tools/openroad-flow-scripts/flow"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
WORK_HOME="${ROOT_DIR}/build/orfs"
FLOW_VARIANT="gcd_baseline20ns"
DESIGN_CONFIG="${ROOT_DIR}/designs/gcd/flow/config.mk"
RESULT_DIR="${WORK_HOME}/results/sky130hd/gcd_power/${FLOW_VARIANT}"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/${FLOW_VARIANT}"
PLACEMENT_ACTIVITY_DIR="${ACTIVITY_DIR}/placement"
ROUTED_ACTIVITY_DIR="${ACTIVITY_DIR}/routed"
REPORT_DIR="${ROOT_DIR}/build/power/${FLOW_VARIANT}"

mkdir -p "${ACTIVITY_DIR}" "${REPORT_DIR}"
make -C "${FLOW_DIR}" DESIGN_CONFIG="${DESIGN_CONFIG}" \
  OPENROAD_EXE="${OPENROAD_EXE}" YOSYS_EXE="${YOSYS_EXE:-$(command -v yosys)}" \
  PYTHON_EXE="${PYTHON_EXE:-$(command -v python3)}" NUM_CORES="${NUM_CORES:-4}" \
  synth floorplan place cts route do-finish

PLACEMENT_NETLIST="${REPORT_DIR}/3_5_place_dp.v" \
PLACEMENT_ODB="${RESULT_DIR}/3_5_place_dp.odb" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_place_netlist.tcl" \
  > "${REPORT_DIR}/export_placement_netlist.log" 2>&1
GATELEVEL_NETLIST="${REPORT_DIR}/3_5_place_dp.v" ACTIVITY_OUT_DIR="${PLACEMENT_ACTIVITY_DIR}" \
  "${ROOT_DIR}/designs/gcd/sim/run_activity.sh" > "${REPORT_DIR}/placement_simulation.log" 2>&1
GATELEVEL_NETLIST="${RESULT_DIR}/6_final.v" ACTIVITY_OUT_DIR="${ROUTED_ACTIVITY_DIR}" \
  "${ROOT_DIR}/designs/gcd/sim/run_activity.sh" > "${REPORT_DIR}/routed_simulation.log" 2>&1

integrity_file="${REPORT_DIR}/integrity.tsv"
printf 'artifact\tsha256_before_reports\tsha256_after_reports\tunchanged\n' > "${integrity_file}"
tracked_artifacts=("${RESULT_DIR}/3_5_place_dp.odb" "${RESULT_DIR}/3_place.sdc" \
  "${RESULT_DIR}/6_final.odb" "${RESULT_DIR}/6_final.sdc" "${RESULT_DIR}/6_final.spef")
input_hashes=()
for artifact in "${tracked_artifacts[@]}"; do
  input_hashes+=("$(shasum -a 256 "${artifact}" | awk '{print $1}')")
done

for workload in idle typical peak; do
  PLACEMENT_ODB="${RESULT_DIR}/3_5_place_dp.odb" \
  PLACEMENT_SDC="${RESULT_DIR}/3_place.sdc" \
  ACTIVITY_VCD="${PLACEMENT_ACTIVITY_DIR}/${workload}.vcd" \
  ACTIVITY_TRACE=1 CAP_REPORT="${REPORT_DIR}/${workload}.driver_caps.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/gcd_report_place_power.tcl" \
    > "${REPORT_DIR}/${workload}.placement.log" 2>&1
  SPEF_ODB="${RESULT_DIR}/6_final.odb" \
  SPEF_SDC="${RESULT_DIR}/6_final.sdc" \
  SPEF_FILE="${RESULT_DIR}/6_final.spef" \
  ACTIVITY_VCD="${ROUTED_ACTIVITY_DIR}/${workload}.vcd" WORKLOAD="${workload}" \
  ACTIVITY_TRACE=1 CAP_REPORT="${REPORT_DIR}/${workload}.routed_driver_caps.tsv" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/gcd_report_spef_power.tcl" \
    > "${REPORT_DIR}/${workload}.routed.log" 2>&1
done

python3 "${ROOT_DIR}/scripts/summarize_activity_cost.py" \
  --report-dir "${REPORT_DIR}" --workloads idle typical peak
for workload in idle typical peak; do
  python3 "${ROOT_DIR}/scripts/export_net_activity_map.py" \
    --input "${REPORT_DIR}/${workload}.per_net_cost.csv" \
    --output "${REPORT_DIR}/${workload}.active_net_density.tsv"
done

for index in "${!tracked_artifacts[@]}"; do
  artifact="${tracked_artifacts[${index}]}"
  before_hash="${input_hashes[${index}]}"
  after_hash="$(shasum -a 256 "${artifact}" | awk '{print $1}')"
  unchanged=false
  if [[ "${before_hash}" == "${after_hash}" ]]; then unchanged=true; fi
  printf '%s\t%s\t%s\t%s\n' "${artifact#${ROOT_DIR}/}" "${before_hash}" \
    "${after_hash}" "${unchanged}" >> "${integrity_file}"
  if [[ "${unchanged}" != true ]]; then
    echo "Read-only power reporting changed source artifact: ${artifact}" >&2
    exit 3
  fi
done

python3 "${ROOT_DIR}/scripts/summarize_gcd_baseline.py" \
  --report-dir "${REPORT_DIR}" --output "${REPORT_DIR}/power_summary.md"
echo "GCD baseline flow and matched activity power reports are in ${REPORT_DIR}"
