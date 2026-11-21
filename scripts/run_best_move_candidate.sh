#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
MOVE_DIR="${ROOT_DIR}/build/power/move_eval"
CANDIDATE_DIR="${ROOT_DIR}/build/power/one_move_candidate"
BASELINE_REFERENCE_DIR="${CANDIDATE_DIR}/baseline_reference"
RESULT_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline/baseline"
BASELINE_ODB="${RESULT_DIR}/3_5_place_dp.odb"
PLACEMENT_SDC="${RESULT_DIR}/3_place.sdc"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/detailed_place"

if [[ ! -x "${OPENROAD_EXE}" || ! -f "${BASELINE_ODB}" || ! -f "${PLACEMENT_SDC}" ]]; then
  echo "Missing OpenROAD build or detailed-placement checkpoint; run ./scripts/run_flow.sh first." >&2
  exit 1
fi

# Refresh all trial data from the current legalized baseline before choosing a move.
"${ROOT_DIR}/scripts/run_place_move_eval.sh"
mkdir -p "${CANDIDATE_DIR}" "${BASELINE_REFERENCE_DIR}"
BASELINE_SHA="$(shasum -a 256 "${BASELINE_ODB}" | awk '{print $1}')"
CANDIDATE_ODB="${CANDIDATE_DIR}/3_5_place_dp_one_move.odb"

ACTIVITY_VCD="${ACTIVITY_DIR}/typical.vcd" \
  MOVE_EVAL_OUTPUT="${MOVE_DIR}/pareto_candidates.tsv" \
  CANDIDATE_ODB="${CANDIDATE_ODB}" \
  MOVE_APPLY_SUMMARY="${CANDIDATE_DIR}/applied_move.tsv" \
  MOVE_EVAL_NONREGRESSION_TOLERANCE_W="${MOVE_EVAL_NONREGRESSION_TOLERANCE_W:-1e-12}" \
  MOVE_SELECTED_INSTANCE="${MOVE_SELECTED_INSTANCE:-}" \
  MOVE_SELECTED_TO_X="${MOVE_SELECTED_TO_X:-}" \
  MOVE_SELECTED_TO_Y="${MOVE_SELECTED_TO_Y:-}" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/apply_best_place_move.tcl" \
  > "${CANDIDATE_DIR}/apply.log" 2>&1

for workload in idle typical peak; do
  CANDIDATE_ODB="${BASELINE_ODB}" \
    PLACEMENT_SDC="${PLACEMENT_SDC}" \
    ACTIVITY_VCD="${ACTIVITY_DIR}/${workload}.vcd" \
    WORKLOAD="${workload}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/verify_place_candidate.tcl" \
    > "${BASELINE_REFERENCE_DIR}/${workload}.log" 2>&1

  CANDIDATE_ODB="${CANDIDATE_ODB}" \
    PLACEMENT_SDC="${PLACEMENT_SDC}" \
    ACTIVITY_VCD="${ACTIVITY_DIR}/${workload}.vcd" \
    WORKLOAD="${workload}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/verify_place_candidate.tcl" \
    > "${CANDIDATE_DIR}/${workload}.log" 2>&1
done

set +e
python3 "${ROOT_DIR}/scripts/summarize_candidate_move.py" \
  --baseline-dir "${BASELINE_REFERENCE_DIR}" \
  --candidate-dir "${CANDIDATE_DIR}" \
  --move-summary "${CANDIDATE_DIR}/applied_move.tsv" \
  --move-table "${MOVE_DIR}/candidate_moves_typical.tsv" \
  --move-dir "${MOVE_DIR}" \
  --output "${CANDIDATE_DIR}/summary.txt"
VERIFY_STATUS=$?
set -e

AFTER_SHA="$(shasum -a 256 "${BASELINE_ODB}" | awk '{print $1}')"
if [[ "${BASELINE_SHA}" != "${AFTER_SHA}" ]]; then
  echo "Baseline ODB changed during candidate generation." >&2
  exit 3
fi
echo "Baseline ODB SHA-256 unchanged: ${AFTER_SHA}"
exit "${VERIFY_STATUS}"
