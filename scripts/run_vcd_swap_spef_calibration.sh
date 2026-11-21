#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUDIT_TAG="${VCD_SWAP_AUDIT_TAG:-20261027_locsearch01}"
AUDIT_DIR="${ROOT_DIR}/build/power/vcd_swap_audit_${AUDIT_TAG}"
PAIR_DIR="${PAIR_CALIBRATION_DIR:-${AUDIT_DIR}/pair_calibration}"
if [[ "${PAIR_DIR}" != /* ]]; then
  PAIR_DIR="${ROOT_DIR}/${PAIR_DIR}"
fi
OPENROAD_EXE="${ROOT_DIR}/tools/openroad/build/bin/openroad"
FLOW_RESULTS="${ROOT_DIR}/build/orfs/results/sky130hd/picorv32_baseline"
BASELINE_RESULT="${FLOW_RESULTS}/baseline"
BASELINE_PLACE_ODB="${BASELINE_RESULT}/3_5_place_dp.odb"
PLACEMENT_SDC="${BASELINE_RESULT}/3_place.sdc"

if [[ ! -x "${OPENROAD_EXE}" || ! -s "${AUDIT_DIR}/integrity.tsv" || \
      ! -s "${BASELINE_PLACE_ODB}" || ! -s "${PLACEMENT_SDC}" ]]; then
  echo "Missing OpenROAD build, completed swap audit, or baseline placement checkpoint." >&2
  exit 1
fi

sha256() {
  shasum -a 256 "$1" | awk '{print $1}'
}

expected_baseline="$(awk -F '\t' '$1 == "baseline_place_odb_sha256_before" {print $2}' "${AUDIT_DIR}/integrity.tsv")"
if [[ -z "${expected_baseline}" || "$(sha256 "${BASELINE_PLACE_ODB}")" != "${expected_baseline}" ]]; then
  echo "Baseline placement ODB no longer matches the swap-audit checkpoint." >&2
  exit 2
fi

mkdir -p "${PAIR_DIR}/placement/baseline" "${PAIR_DIR}/placement/swap44" "${PAIR_DIR}/placement/swap52"

declare -a INPUTS=("${BASELINE_PLACE_ODB}" "${PLACEMENT_SDC}" "${BASELINE_RESULT}/6_final.odb" "${BASELINE_RESULT}/6_final.spef")
for candidate in swap44 swap52; do
  candidate_odb="${AUDIT_DIR}/candidate_odb/${candidate}.odb"
  candidate_expected="$(awk -F '\t' -v key="candidate_${candidate}_odb_sha256_before_after" '$1 == key {print $2}' "${AUDIT_DIR}/integrity.tsv")"
  if [[ -z "${candidate_expected}" || "$(sha256 "${candidate_odb}")" != "${candidate_expected}" ]]; then
    echo "${candidate} placement ODB no longer matches the swap-audit checkpoint." >&2
    exit 2
  fi
  variant="vcdswap_${AUDIT_TAG}_${candidate}"
  report_dir="${ROOT_DIR}/build/power/${variant}"
  activity_dir="${ROOT_DIR}/build/activity/${variant}"
  result_dir="${FLOW_RESULTS}/${variant}"
  if [[ ! -s "${report_dir}/route_complete.txt" || ! -s "${result_dir}/6_final.odb" || ! -s "${result_dir}/6_final.spef" ]]; then
    echo "Missing completed routed ODB/SPEF for ${candidate} (${variant})." >&2
    exit 1
  fi
  INPUTS+=("${candidate_odb}" "${result_dir}/6_final.odb" "${result_dir}/6_final.spef")
  for workload in idle typical peak; do
    INPUTS+=("${activity_dir}/candidate/${workload}.vcd")
  done
done

BASELINE_ACTIVITY="${ROOT_DIR}/build/activity/vcdswap_${AUDIT_TAG}_swap44/baseline"
for workload in idle typical peak; do
  INPUTS+=("${BASELINE_ACTIVITY}/${workload}.vcd")
done

snapshot_inputs() {
  local destination="$1"
  shift
  : > "${destination}"
  for path in "$@"; do
    if [[ ! -s "${path}" ]]; then
      echo "Missing calibration input: ${path}" >&2
      exit 1
    fi
    printf '%s\t%s\n' "$(sha256 "${path}")" "${path}" >> "${destination}"
  done
}

snapshot_inputs "${PAIR_DIR}/input_hashes_before.tsv" "${INPUTS[@]}"

for workload in idle typical peak; do
  ACTIVITY_VCD="${BASELINE_ACTIVITY}/${workload}.vcd" ACTIVITY_TRACE=1 \
    CAP_REPORT="${PAIR_DIR}/placement/baseline/${workload}.driver_caps.tsv" \
    PLACEMENT_ODB="${BASELINE_PLACE_ODB}" PLACEMENT_SDC="${PLACEMENT_SDC}" \
    "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_place_power.tcl" \
    > "${PAIR_DIR}/placement/baseline/${workload}.log" 2>&1
  for candidate in swap44 swap52; do
    variant="vcdswap_${AUDIT_TAG}_${candidate}"
    ACTIVITY_VCD="${ROOT_DIR}/build/activity/${variant}/candidate/${workload}.vcd" ACTIVITY_TRACE=1 \
      CAP_REPORT="${PAIR_DIR}/placement/${candidate}/${workload}.driver_caps.tsv" \
      PLACEMENT_ODB="${AUDIT_DIR}/candidate_odb/${candidate}.odb" PLACEMENT_SDC="${PLACEMENT_SDC}" \
      "${OPENROAD_EXE}" -no_init -exit "${ROOT_DIR}/scripts/report_place_power.tcl" \
      > "${PAIR_DIR}/placement/${candidate}/${workload}.log" 2>&1
  done
done

VCD_SWAP_AUDIT_TAG="${AUDIT_TAG}" PAIR_CALIBRATION_DIR="${PAIR_DIR}" \
  python3 "${ROOT_DIR}/scripts/export_vcd_swap_calibration_features.py"
VCD_SWAP_AUDIT_TAG="${AUDIT_TAG}" python3 "${ROOT_DIR}/scripts/analyze_vcd_swap_spef_calibration.py" \
  --pair-dir "${PAIR_DIR}" --output-dir "${PAIR_DIR}/analysis"

snapshot_inputs "${PAIR_DIR}/input_hashes_after.tsv" "${INPUTS[@]}"
if ! cmp -s "${PAIR_DIR}/input_hashes_before.tsv" "${PAIR_DIR}/input_hashes_after.tsv"; then
  echo "A calibration source input changed during the read-only placement/SPEF analysis." >&2
  exit 3
fi

echo "Calibration input hashes match before and after analysis."
echo "Report: ${PAIR_DIR}/analysis/calibration_report.md"
