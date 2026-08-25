#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
ENV_FILE="${CUSTOMER_SETTLEMENTS_ENV_FILE:-${REPO_DIR}/.env}"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
RETRY_DELAY_SECONDS="${CUSTOMER_SETTLEMENTS_RETRY_DELAY_SECONDS:-600}"
JOB_TIMEOUT_SECONDS="${CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS:-90}"

cd "${REPO_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

run_sync() {
  timeout --signal=TERM --kill-after=5s "${JOB_TIMEOUT_SECONDS}s" \
    "${PYTHON_BIN}" -m tasks.sync_customer_settlement_mapping
}

run_sync
first_exit_code=$?
if (( first_exit_code == 0 )); then
  exit 0
fi
if (( first_exit_code == 2 )); then
  exit "${first_exit_code}"
fi

sleep "${RETRY_DELAY_SECONDS}"
run_sync
