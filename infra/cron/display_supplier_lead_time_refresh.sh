#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/display_supplier_lead_time_refresh.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_display_supplier_lead_time_refresh.lock}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

mkdir -p "${LOG_DIR}"
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

FOLDER="${DISPLAY_SUPPLIER_LEAD_TIME_FOLDER:-дисплеи}"
AS_OF="${DISPLAY_SUPPLIER_LEAD_TIME_AS_OF:-$(date +%F)}"
HISTORY_MONTHS="${DISPLAY_SUPPLIER_LEAD_TIME_HISTORY_MONTHS:-36}"
REPORT_DIR="${DISPLAY_SUPPLIER_LEAD_TIME_REPORT_DIR:-${REPO_DIR}/reports/assortment_lifecycle/${AS_OF}}"
OUTPUT_CSV="${DISPLAY_SUPPLIER_LEAD_TIME_OUTPUT_CSV:-${REPORT_DIR}/display-supplier-lead-time-history.csv}"
OUTPUT_DETAIL_CSV="${DISPLAY_SUPPLIER_LEAD_TIME_OUTPUT_DETAIL_CSV:-${REPORT_DIR}/display-supplier-lead-time-history-detail.csv}"
OUTPUT_JSON="${DISPLAY_SUPPLIER_LEAD_TIME_OUTPUT_JSON:-${REPORT_DIR}/display-supplier-lead-time-history-summary.json}"
OUTPUT_SEASONALITY_CSV="${DISPLAY_SUPPLIER_LEAD_TIME_OUTPUT_SEASONALITY_CSV:-${REPORT_DIR}/display-supplier-lead-time-seasonality.csv}"
OUTPUT_SEASONALITY_JSON="${DISPLAY_SUPPLIER_LEAD_TIME_OUTPUT_SEASONALITY_JSON:-${REPORT_DIR}/display-supplier-lead-time-seasonality-summary.json}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] display supplier lead-time refresh skipped: another run is still active" >> "${LOG_FILE}"
  exit 0
fi

mkdir -p "${REPORT_DIR}"

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting display supplier lead-time refresh folder=${FOLDER} as_of=${AS_OF} history_months=${HISTORY_MONTHS}" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${PYTHON_BIN}" -m tasks.report_display_supplier_lead_time_history \
  --folder "${FOLDER}" \
  --history-months "${HISTORY_MONTHS}" \
  --as-of "${AS_OF}" \
  --output-csv "${OUTPUT_CSV}" \
  --output-detail-csv "${OUTPUT_DETAIL_CSV}" \
  --output-json "${OUTPUT_JSON}" \
  --output-seasonality-csv "${OUTPUT_SEASONALITY_CSV}" \
  --output-seasonality-json "${OUTPUT_SEASONALITY_JSON}" \
  --json \
  > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished display supplier lead-time refresh (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
