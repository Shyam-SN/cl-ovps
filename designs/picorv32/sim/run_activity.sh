#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
DESIGN_DIR="${ROOT_DIR}/designs/picorv32"
OUT_DIR="${ACTIVITY_OUT_DIR:-${ROOT_DIR}/build/activity/rtl}"
TESTBENCH="${DESIGN_DIR}/sim/tb_picorv32_activity.sv"

mkdir -p "${OUT_DIR}"

if [[ -n "${GATELEVEL_NETLIST:-}" ]]; then
  CELL_MODEL="${SKY130_CELL_MODEL:-${ROOT_DIR}/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v}"
  PRIMITIVES="${SKY130_PRIMITIVES:-${ROOT_DIR}/pdk/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v}"
  if [[ ! -f "${GATELEVEL_NETLIST}" ]]; then
    echo "GATELEVEL_NETLIST does not exist: ${GATELEVEL_NETLIST}" >&2
    exit 2
  fi
  if [[ ! -f "${CELL_MODEL}" ]]; then
    echo "SKY130 functional model does not exist: ${CELL_MODEL}" >&2
    exit 2
  fi
  if [[ ! -f "${PRIMITIVES}" ]]; then
    echo "SKY130 primitive models do not exist: ${PRIMITIVES}" >&2
    exit 2
  fi
  iverilog -g2012 -DFUNCTIONAL -s tb_picorv32_activity -o "${OUT_DIR}/sim.vvp" \
    "${PRIMITIVES}" "${CELL_MODEL}" "${GATELEVEL_NETLIST}" "${TESTBENCH}"
else
  iverilog -g2012 -s tb_picorv32_activity -o "${OUT_DIR}/sim.vvp" \
    "${DESIGN_DIR}/picorv32.v" \
    "${DESIGN_DIR}/flow/rtl/picorv32_top.v" \
    "${TESTBENCH}"
fi

cd "${OUT_DIR}"
for workload in idle typical peak; do
  vvp ./sim.vvp "+workload=${workload}"
  mv activity.vcd "${workload}.vcd"
done
