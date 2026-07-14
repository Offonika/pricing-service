#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/open_procurement_supplier_orders_sync.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_open_procurement_supplier_orders_sync.lock}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

CONTOURS="${PROCUREMENT_SYNC_CONTOURS:-ved_import}"
DAYS_BACK="${PROCUREMENT_SYNC_DAYS_BACK:-10}"
LIMIT="${PROCUREMENT_SYNC_LIMIT:-500}"
ASSIGNED_BY_ID="${PROCUREMENT_SYNC_ASSIGNED_BY_ID:-130750}"
APPLY="${PROCUREMENT_SYNC_APPLY:-true}"
BLANK_CONTOUR_CARGO_DROPOFF_SYNC="${PROCUREMENT_SYNC_BLANK_CONTOUR_CARGO_DROPOFF:-true}"
BLANK_CONTOUR_CARGO_DROPOFF_LIMIT="${PROCUREMENT_SYNC_BLANK_CONTOUR_CARGO_DROPOFF_LIMIT:-5000}"
BLANK_CONTOUR_CARGO_DROPOFF_DATE_FROM="${PROCUREMENT_SYNC_BLANK_CONTOUR_CARGO_DROPOFF_DATE_FROM:-}"
BLANK_CONTOUR_CARGO_DROPOFF_DATE_TO="${PROCUREMENT_SYNC_BLANK_CONTOUR_CARGO_DROPOFF_DATE_TO:-}"
LEAD_TIME_REFRESH_ON_CARGO_DROPOFF="${PROCUREMENT_SYNC_LEAD_TIME_REFRESH_ON_CARGO_DROPOFF:-true}"
LEAD_TIME_REFRESH_SCRIPT="${PROCUREMENT_SYNC_LEAD_TIME_REFRESH_SCRIPT:-${REPO_DIR}/infra/cron/display_supplier_lead_time_refresh.sh}"
LEAD_TIME_REFRESH_STATE_PATH="${PROCUREMENT_SYNC_LEAD_TIME_REFRESH_STATE_PATH:-${REPO_DIR}/build/bitrix/procurement_cargo_dropoff_lead_time_refresh_state.json}"

mkdir -p "${LOG_DIR}" "${REPO_DIR}/build/bitrix"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] open procurement supplier orders sync skipped: another run is still active" >> "${LOG_FILE}"
  exit 0
fi

date_from="${PROCUREMENT_SYNC_DATE_FROM:-$(date -d "${DAYS_BACK} days ago" +%F)}"
date_to="${PROCUREMENT_SYNC_DATE_TO:-$(date -d tomorrow +%F)}"

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting open procurement supplier orders sync contours=${CONTOURS} date_from=${date_from} date_to=${date_to} apply=${APPLY}" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

is_truthy() {
  case "${1,,}" in
    true|1|yes|y|да)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

tmp_output="$(mktemp)"
SYNC_RESULT_PATHS=()
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

run_sync() {
  local label="$1"
  local contours="$2"
  local limit="$3"
  local input_json="$4"
  local result_path="$5"
  local date_from_arg="${6:-}"
  local date_to_arg="${7:-}"
  shift 7 || true

  local cmd=(
    "${PYTHON_BIN}"
    scripts/sync_open_cargo_supplier_orders_to_bitrix.py
    --contours "${contours}"
    --limit "${limit}"
    --assigned-by-id "${ASSIGNED_BY_ID}"
    --input-json "${input_json}"
    --result-path "${result_path}"
  )
  if [[ -n "${date_from_arg}" ]]; then
    cmd+=(--date-from "${date_from_arg}")
  fi
  if [[ -n "${date_to_arg}" ]]; then
    cmd+=(--date-to "${date_to_arg}")
  fi
  while (($#)); do
    cmd+=("$1")
    shift
  done
  if is_truthy "${APPLY}"; then
    cmd+=(--apply)
  fi

  echo "[$(date -Iseconds)] running ${label}: ${cmd[*]}" >> "${LOG_FILE}"
  set +e
  "${cmd[@]}" > "${tmp_output}" 2>&1
  local exit_code=$?
  set -e
  cat "${tmp_output}" >> "${LOG_FILE}"
  echo "[$(date -Iseconds)] finished ${label} (status=${exit_code})" >> "${LOG_FILE}"
  if [[ "${exit_code}" -eq 0 ]]; then
    SYNC_RESULT_PATHS+=("${result_path}")
  fi
  return "${exit_code}"
}

maybe_refresh_supplier_lead_time_after_cargo_dropoff() {
  if ! is_truthy "${LEAD_TIME_REFRESH_ON_CARGO_DROPOFF}"; then
    echo "[$(date -Iseconds)] cargo dropoff lead-time refresh disabled" >> "${LOG_FILE}"
    return 0
  fi
  if [[ "${#SYNC_RESULT_PATHS[@]}" -eq 0 ]]; then
    echo "[$(date -Iseconds)] cargo dropoff lead-time refresh skipped: no successful sync results" >> "${LOG_FILE}"
    return 0
  fi

  local detector_cmd=(
    "${PYTHON_BIN}"
    -m tasks.detect_procurement_cargo_dropoff_refresh_needed
    --state-path "${LEAD_TIME_REFRESH_STATE_PATH}"
  )
  for result_path in "${SYNC_RESULT_PATHS[@]}"; do
    detector_cmd+=(--result-json "${result_path}")
  done

  echo "[$(date -Iseconds)] checking cargo dropoff lead-time trigger: ${detector_cmd[*]}" >> "${LOG_FILE}"
  set +e
  local detection_output
  detection_output="$("${detector_cmd[@]}" --format env 2>&1)"
  local detector_status=$?
  set -e
  printf '%s\n' "${detection_output}" >> "${LOG_FILE}"
  if [[ "${detector_status}" -ne 0 ]]; then
    echo "[$(date -Iseconds)] cargo dropoff lead-time trigger failed (status=${detector_status})" >> "${LOG_FILE}"
    return "${detector_status}"
  fi

  local refresh_needed
  refresh_needed="$(printf '%s\n' "${detection_output}" | awk -F= '$1=="refresh_needed"{print $2; exit}')"
  if [[ "${refresh_needed}" != "true" ]]; then
    echo "[$(date -Iseconds)] cargo dropoff lead-time refresh skipped: no new cargo dropoff dates" >> "${LOG_FILE}"
    return 0
  fi

  echo "[$(date -Iseconds)] new cargo dropoff dates detected; running supplier lead-time refresh" >> "${LOG_FILE}"
  set +e
  "${LEAD_TIME_REFRESH_SCRIPT}" >> "${LOG_FILE}" 2>&1
  local refresh_status=$?
  set -e
  if [[ "${refresh_status}" -ne 0 ]]; then
    echo "[$(date -Iseconds)] supplier lead-time refresh failed (status=${refresh_status})" >> "${LOG_FILE}"
    return "${refresh_status}"
  fi

  echo "[$(date -Iseconds)] saving cargo dropoff lead-time trigger state" >> "${LOG_FILE}"
  "${detector_cmd[@]}" --apply-state >> "${LOG_FILE}" 2>&1
}

run_sync \
  "primary" \
  "${CONTOURS}" \
  "${LIMIT}" \
  "${REPO_DIR}/build/bitrix/onec_open_procurement_supplier_orders_input.json" \
  "${REPO_DIR}/build/bitrix/onec_open_procurement_supplier_orders_result.json" \
  "${date_from}" \
  "${date_to}"
exit_code=$?

if [[ "${exit_code}" -eq 0 ]] && is_truthy "${BLANK_CONTOUR_CARGO_DROPOFF_SYNC}"; then
  run_sync \
    "blank-contour-cargo-dropoff" \
    "cargo" \
    "${BLANK_CONTOUR_CARGO_DROPOFF_LIMIT}" \
    "${REPO_DIR}/build/bitrix/onec_blank_contour_cargo_dropoff_orders_input.json" \
    "${REPO_DIR}/build/bitrix/onec_blank_contour_cargo_dropoff_orders_result.json" \
    "${BLANK_CONTOUR_CARGO_DROPOFF_DATE_FROM}" \
    "${BLANK_CONTOUR_CARGO_DROPOFF_DATE_TO}" \
    --blank-contour-cargo-dropoff-only
  exit_code=$?
fi

if [[ "${exit_code}" -eq 0 ]]; then
  maybe_refresh_supplier_lead_time_after_cargo_dropoff
  exit_code=$?
fi

timestamp="$(date -Iseconds)"
echo "[$timestamp] finished open procurement supplier orders sync (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
