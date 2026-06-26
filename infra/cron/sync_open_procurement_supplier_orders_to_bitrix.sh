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

cmd=(
  "${PYTHON_BIN}"
  scripts/sync_open_cargo_supplier_orders_to_bitrix.py
  --contours "${CONTOURS}"
  --date-from "${date_from}"
  --date-to "${date_to}"
  --limit "${LIMIT}"
  --assigned-by-id "${ASSIGNED_BY_ID}"
  --input-json "${REPO_DIR}/build/bitrix/onec_open_procurement_supplier_orders_input.json"
  --result-path "${REPO_DIR}/build/bitrix/onec_open_procurement_supplier_orders_result.json"
)

if [[ "${APPLY}" == "true" || "${APPLY}" == "1" || "${APPLY}" == "yes" ]]; then
  cmd+=(--apply)
fi

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${cmd[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished open procurement supplier orders sync (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
