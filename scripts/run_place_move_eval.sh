#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
PLACEMENT_ODB="${RESULT_DIR}/3_5_place_dp.odb"
PLACEMENT_SDC="${RESULT_DIR}/3_place.sdc"
NETLIST="${ROOT_DIR}/build/activity/netlists/3_5_place_dp.v"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/detailed_place"
POWER_DIR="${ROOT_DIR}/build/power/detailed_place"
MOVE_DIR="${ROOT_DIR}/build/power/move_eval"

if [[ ! -f "${PLACEMENT_ODB}" || ! -f "${PLACEMENT_SDC}" ]]; then
  echo "Missing detailed-placement checkpoint; run ./scripts/run_flow.sh first." >&2
  exit 1
fi

mkdir -p "$(dirname "${NETLIST}")" "${ACTIVITY_DIR}" "${POWER_DIR}" "${MOVE_DIR}"
PLACEMENT_ODB="${PLACEMENT_ODB}" PLACEMENT_NETLIST="${NETLIST}" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_place_netlist.tcl" \
  > "${POWER_DIR}/export_netlist.log" 2>&1

GATELEVEL_NETLIST="${NETLIST}" ACTIVITY_OUT_DIR="${ACTIVITY_DIR}" \
  "${ROOT_DIR}/designs/picorv32/sim/run_activity.sh" \
  > "${POWER_DIR}/simulation.log" 2>&1

for workload in idle typical peak; do
  ACTIVITY_VCD="${ACTIVITY_DIR}/${workload}.vcd" \
    ACTIVITY_TRACE=1 \
    CAP_REPORT="${POWER_DIR}/${workload}.driver_caps.tsv" \
    PLACEMENT_ODB="${PLACEMENT_ODB}" \
    PLACEMENT_SDC="${PLACEMENT_SDC}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_place_power.tcl" \
    > "${POWER_DIR}/${workload}.log" 2>&1
done

python3 "${ROOT_DIR}/scripts/summarize_activity_cost.py" \
  --report-dir "${POWER_DIR}" \
  --workloads idle typical peak

MOVE_EVAL_MAX_CELLS="${MOVE_EVAL_MAX_CELLS:-6}"
python3 "${ROOT_DIR}/scripts/prepare_move_eval_inputs.py" \
  --idle-cost-csv "${POWER_DIR}/idle.per_net_cost.csv" \
  --typical-cost-csv "${POWER_DIR}/typical.per_net_cost.csv" \
  --peak-cost-csv "${POWER_DIR}/peak.per_net_cost.csv" \
  --candidate-file "${MOVE_DIR}/candidate_cells.tsv" \
  --activity-dir "${MOVE_DIR}/activity" \
  --max-cells "${MOVE_EVAL_MAX_CELLS}"

for workload in idle typical peak; do
  ACTIVITY_VCD="${ACTIVITY_DIR}/${workload}.vcd" \
    MOVE_EVAL_CANDIDATES="${MOVE_DIR}/candidate_cells.tsv" \
    MOVE_EVAL_NET_ACTIVITY="${MOVE_DIR}/activity/${workload}.active_net_density.tsv" \
    MOVE_EVAL_OUTPUT="${MOVE_DIR}/candidate_moves_${workload}.tsv" \
    MOVE_EVAL_WORKLOAD="${workload}" \
    MOVE_EVAL_MAX_TIMING_LOSS_NS="${MOVE_EVAL_MAX_TIMING_LOSS_NS:-0.05}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/evaluate_place_moves.tcl" \
    > "${MOVE_DIR}/evaluator_${workload}.log" 2>&1
done

python3 "${ROOT_DIR}/scripts/summarize_multiworkload_moves.py" \
  --move-dir "${MOVE_DIR}" \
  --pareto "${MOVE_DIR}/pareto_candidates.tsv" \
  --summary "${MOVE_DIR}/summary.txt"

echo "Multi-workload move reports written to ${MOVE_DIR}"
