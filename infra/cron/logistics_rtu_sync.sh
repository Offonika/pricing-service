#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/logistics_rtu_sync.log}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
LOOKBACK_DAYS="${LOGISTICS_RTU_SYNC_LOOKBACK_DAYS:-14}"
LIMIT="${LOGISTICS_RTU_SYNC_LIMIT:-500}"
APPLY="${LOGISTICS_RTU_SYNC_APPLY:-false}"
LOCK_FILE="${LOGISTICS_RTU_SYNC_LOCK_FILE:-/tmp/logistics_rtu_sync.lock}"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] logistics_rtu_sync skipped: previous run is active" >> "${LOG_FILE}"
  exit 0
fi

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(command -v python3 || command -v python)"
fi

DATE_FROM="$(date -u -d "${LOOKBACK_DAYS} days ago" +%F)"
cmd=(
  "${PYTHON_BIN}" -m tasks.sync_logistics_rtu_from_onec
  --date-from "${DATE_FROM}"
  --limit "${LIMIT}"
)
if [[ "${APPLY}" == "true" ]]; then
  cmd+=(--apply)
fi

echo "[$(date -Iseconds)] starting logistics_rtu_sync apply=${APPLY} date_from=${DATE_FROM} limit=${LIMIT}" >> "${LOG_FILE}"
"${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] logistics_rtu_sync finished" >> "${LOG_FILE}"
