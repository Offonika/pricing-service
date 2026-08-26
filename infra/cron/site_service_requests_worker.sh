#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/site_service_requests_worker.log}"
LOCK_FILE="${LOCK_FILE:-/run/lock/site_service_requests_worker.lock}"
TIMEOUT_SECONDS="${SITE_SERVICE_REQUESTS_WORKER_TIMEOUT_SECONDS:-600}"
START_DELAY_SECONDS="${SITE_SERVICE_REQUESTS_WORKER_START_DELAY_SECONDS:-20}"

mkdir -p "${LOG_DIR}" "$(dirname "${LOCK_FILE}")"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] site_service_requests_worker skipped: lock busy" >> "${LOG_FILE}"
  exit 0
fi

if ! [[ "${START_DELAY_SECONDS}" =~ ^([0-9]|[1-4][0-9]|5[0-5])$ ]]; then
  echo "[$(date -Iseconds)] site_service_requests_worker invalid start delay" >> "${LOG_FILE}"
  exit 2
fi
if [[ "${START_DELAY_SECONDS}" != "0" ]]; then
  echo "[$(date -Iseconds)] site_service_requests_worker delaying ${START_DELAY_SECONDS}s" >> "${LOG_FILE}"
  sleep "${START_DELAY_SECONDS}"
fi

cmd=("${PYTHON_BIN}" -m tasks.site_service_requests_worker --compact)
if [[ "${SITE_SERVICE_REQUESTS_WORKER_APPLY:-false}" == "true" ]]; then
  cmd+=(--apply)
fi

echo "[$(date -Iseconds)] site_service_requests_worker starting apply=${SITE_SERVICE_REQUESTS_WORKER_APPLY:-false}" >> "${LOG_FILE}"
timeout "${TIMEOUT_SECONDS}" "${PYTHON_BIN}" -m tasks.site_service_requests_worker --check --compact >> "${LOG_FILE}" 2>&1
timeout "${TIMEOUT_SECONDS}" "${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] site_service_requests_worker finished" >> "${LOG_FILE}"
