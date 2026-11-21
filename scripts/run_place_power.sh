#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
NETLIST="${ROOT_DIR}/build/activity/netlists/3_3_place_gp.v"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/placement"
REPORT_DIR="${ROOT_DIR}/build/power/placement"

mkdir -p "$(dirname "${NETLIST}")" "${ACTIVITY_DIR}" "${REPORT_DIR}"
PLACEMENT_NETLIST="${NETLIST}" "${OPENROAD_EXE}" -no_init -exit \
  "${ROOT_DIR}/scripts/export_place_netlist.tcl" \
  > "${REPORT_DIR}/export_netlist.log" 2>&1

GATELEVEL_NETLIST="${NETLIST}" ACTIVITY_OUT_DIR="${ACTIVITY_DIR}" \
  "${ROOT_DIR}/designs/picorv32/sim/run_activity.sh" \
  > "${REPORT_DIR}/simulation.log" 2>&1

for workload in idle typical peak; do
  ACTIVITY_VCD="${ACTIVITY_DIR}/${workload}.vcd" \
    ACTIVITY_TRACE=1 \
    CAP_REPORT="${REPORT_DIR}/${workload}.driver_caps.tsv" \
    "${OPENROAD_EXE}" -no_init -exit \
    "${ROOT_DIR}/scripts/report_place_power.tcl" \
    > "${REPORT_DIR}/${workload}.log" 2>&1
done

python3 "${ROOT_DIR}/scripts/summarize_activity_cost.py" \
  --report-dir "${REPORT_DIR}" \
  --workloads idle typical peak

echo "Placement-stage power reports written to ${REPORT_DIR}"
