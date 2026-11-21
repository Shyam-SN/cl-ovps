#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DESIGN_DIR="${ROOT_DIR}/designs/gcd"
OUT_DIR="${ACTIVITY_OUT_DIR:-${ROOT_DIR}/build/activity/gcd}"
TESTBENCH="${DESIGN_DIR}/sim/tb_gcd_activity.sv"
mkdir -p "${OUT_DIR}"

if [[ -n "${GATELEVEL_NETLIST:-}" ]]; then
  CELL_MODEL="${SKY130_CELL_MODEL:-${ROOT_DIR}/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v}"
  PRIMITIVES="${SKY130_PRIMITIVES:-${ROOT_DIR}/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v}"
  iverilog -g2012 -DFUNCTIONAL -s tb_gcd_activity -o "${OUT_DIR}/sim.vvp" \
    "${PRIMITIVES}" "${CELL_MODEL}" "${GATELEVEL_NETLIST}" "${TESTBENCH}"
else
  iverilog -g2012 -s tb_gcd_activity -o "${OUT_DIR}/sim.vvp" \
    "${DESIGN_DIR}/gcd.v" "${TESTBENCH}"
fi

cd "${OUT_DIR}"
for workload in 0 1 2; do
  vvp ./sim.vvp "+workload=${workload}"
  case "${workload}" in
    0) mv activity.vcd idle.vcd ;;
    1) mv activity.vcd typical.vcd ;;
    2) mv activity.vcd peak.vcd ;;
  esac
done
