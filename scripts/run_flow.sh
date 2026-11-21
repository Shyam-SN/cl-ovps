#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FLOW_DIR="${ROOT_DIR}/tools/openroad-flow-scripts/flow"
DESIGN_CONFIG="${ROOT_DIR}/designs/picorv32/flow/config.mk"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
YOSYS_EXE="${YOSYS_EXE:-$(command -v yosys)}"
PYTHON_EXE="${PYTHON_EXE:-$(command -v python3)}"

MAKE_TARGETS=("$@")
if [[ ${#MAKE_TARGETS[@]} -eq 0 ]]; then
  # Build the routed baseline and final reports without requiring the optional
  # KLayout-based GDS target.
  MAKE_TARGETS=(synth floorplan place cts route do-finish)
fi

exec make -C "${FLOW_DIR}" \
  DESIGN_CONFIG="${DESIGN_CONFIG}" \
  OPENROAD_EXE="${OPENROAD_EXE}" \
  YOSYS_EXE="${YOSYS_EXE}" \
  PYTHON_EXE="${PYTHON_EXE}" \
  NUM_CORES="${NUM_CORES:-4}" \
  "${MAKE_TARGETS[@]}"
