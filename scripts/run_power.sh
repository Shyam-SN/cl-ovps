#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
TCL_SCRIPT="${ROOT_DIR}/scripts/report_power.tcl"
VCD_DIR="${ACTIVITY_VCD_DIR:-${ROOT_DIR}/build/activity/gatelevel}"
REPORT_DIR="${POWER_REPORT_DIR:-${ROOT_DIR}/build/power/baseline}"

mkdir -p "${REPORT_DIR}"
for workload in idle typical peak; do
  ACTIVITY_VCD="${VCD_DIR}/${workload}.vcd" \
    "${OPENROAD_EXE}" -no_init -exit "${TCL_SCRIPT}" \
    > "${REPORT_DIR}/${workload}.log" 2>&1
done

echo "Power reports written to ${REPORT_DIR}"
