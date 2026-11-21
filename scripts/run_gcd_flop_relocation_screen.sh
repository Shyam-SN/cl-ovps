#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
BASE_DIR="${ROOT_DIR}/build/orfs/results/sky130hd/gcd_power/gcd_baseline20ns"
ACTIVITY_DIR="${ROOT_DIR}/build/activity/gcd_baseline20ns/placement"
TAG="${RELOCATION_TAG:-$(date +%Y%m%d_%H%M%S)}"
OUT_DIR="${ROOT_DIR}/build/power/gcd_flop_relocation_${TAG}"
ODB_DIR="${OUT_DIR}/candidate_odb"
INVENTORY="${OUT_DIR}/inventory.tsv"
POOL="${OUT_DIR}/candidate_pool.tsv"
AUDIT="${OUT_DIR}/relocation_audit.tsv"
LOG="${OUT_DIR}/evaluator.log"

for path in "${OPENROAD_EXE}" "${BASE_DIR}/3_5_place_dp.odb" "${BASE_DIR}/3_place.sdc" \
  "${ACTIVITY_DIR}/idle.vcd" "${ACTIVITY_DIR}/typical.vcd" "${ACTIVITY_DIR}/peak.vcd"; do
  [[ -s "${path}" ]] || { echo "Missing required relocation input: ${path}" >&2; exit 1; }
done
[[ ! -e "${OUT_DIR}" ]] || { echo "Output already exists: ${OUT_DIR}" >&2; exit 1; }
mkdir -p "${ODB_DIR}"

BASELINE_SHA="$(shasum -a 256 "${BASE_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
PLACEMENT_ODB="${BASE_DIR}/3_5_place_dp.odb" SWAP_INVENTORY_OUT="${INVENTORY}" \
  "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_vcd_swap_inventory.tcl" \
  > "${OUT_DIR}/inventory.log" 2>&1
if [[ -n "${RELOCATION_RANDOM_SEED:-}" ]]; then
  OCCUPANCY_INVENTORY="${OUT_DIR}/occupancy_inventory.tsv"
  PLACEMENT_ODB="${BASE_DIR}/3_5_place_dp.odb" \
  RELOCATION_INVENTORY_OUT="${OCCUPANCY_INVENTORY}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/export_gcd_relocation_inventory.tcl" \
    > "${OUT_DIR}/occupancy_inventory.log" 2>&1
  EXCLUDE_ARGS=()
  if [[ -n "${RELOCATION_EXCLUDE_POOLS:-}" ]]; then
    IFS=: read -r -a EXCLUDE_POOL_PATHS <<< "${RELOCATION_EXCLUDE_POOLS}"
    for exclude_pool in "${EXCLUDE_POOL_PATHS[@]}"; do
      EXCLUDE_ARGS+=(--exclude-pool "${exclude_pool}")
    done
  fi
  python3 "${ROOT_DIR}/scripts/make_gcd_random_flop_relocation_pool.py" \
    --inventory "${OCCUPANCY_INVENTORY}" --output "${POOL}" \
    --count "${RELOCATION_CANDIDATE_COUNT:-35}" --seed "${RELOCATION_RANDOM_SEED}" \
    --min-move-dbu "${RELOCATION_MIN_MOVE_DBU:-4000}" \
    --max-move-dbu "${RELOCATION_MAX_MOVE_DBU:-20000}" \
    "${EXCLUDE_ARGS[@]}" \
    > "${OUT_DIR}/pool_generation.log"
else
  python3 "${ROOT_DIR}/scripts/make_gcd_flop_relocation_pool.py" \
    --inventory "${INVENTORY}" --output "${POOL}" --count "${RELOCATION_CANDIDATE_COUNT:-16}" \
    > "${OUT_DIR}/pool_generation.log"
fi

PLACEMENT_ODB="${BASE_DIR}/3_5_place_dp.odb" \
PLACEMENT_SDC="${BASE_DIR}/3_place.sdc" \
RELOCATION_POOL="${POOL}" \
RELOCATION_AUDIT="${AUDIT}" \
CANDIDATE_ODB_DIR="${ODB_DIR}" \
IDLE_VCD="${ACTIVITY_DIR}/idle.vcd" \
TYPICAL_VCD="${ACTIVITY_DIR}/typical.vcd" \
PEAK_VCD="${ACTIVITY_DIR}/peak.vcd" \
"${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/evaluate_gcd_flop_relocations.tcl" \
  > "${LOG}" 2>&1

AFTER_SHA="$(shasum -a 256 "${BASE_DIR}/3_5_place_dp.odb" | awk '{print $1}')"
printf 'baseline_placement_odb_before\t%s\nbaseline_placement_odb_after\t%s\n' \
  "${BASELINE_SHA}" "${AFTER_SHA}" > "${OUT_DIR}/integrity.tsv"
[[ "${BASELINE_SHA}" == "${AFTER_SHA}" ]] || { echo "Baseline placement ODB changed." >&2; exit 2; }
echo "Relocation screen complete: ${AUDIT}"
echo "Baseline placement ODB hash unchanged: ${BASELINE_SHA}"
