#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

SUCCESS_FILE="${LOGISTICS_RTU_SYNC_SUCCESS_FILE:-/var/lib/pricing-service/logistics_rtu_sync.last_success}"
MAX_AGE_SECONDS="${LOGISTICS_RTU_SYNC_MAX_AGE_SECONDS:-180}"
STAGE_MAX_DELAY_SECONDS="${LOGISTICS_STAGE_OUTBOX_MAX_DELAY_SECONDS:-30}"
REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"

if [[ ! -f "${SUCCESS_FILE}" ]]; then
  echo "[$(date -Iseconds)] CRITICAL logistics_rtu_sync has no successful-run marker"
  exit 1
fi

now_epoch="$(date +%s)"
success_epoch="$(stat -c %Y "${SUCCESS_FILE}")"
age_seconds="$(( now_epoch - success_epoch ))"
if [[ "${age_seconds}" -gt "${MAX_AGE_SECONDS}" ]]; then
  echo "[$(date -Iseconds)] CRITICAL logistics_rtu_sync is stale age_seconds=${age_seconds} max_age_seconds=${MAX_AGE_SECONDS}"
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "[$(date -Iseconds)] CRITICAL logistics stage-outbox check has no release Python"
  exit 1
fi

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

PYTHONPATH="${REPO_DIR}" "${PYTHON_BIN}" -m tasks.check_logistics_stage_outbox_health \
  --max-delay-seconds "${STAGE_MAX_DELAY_SECONDS}"
