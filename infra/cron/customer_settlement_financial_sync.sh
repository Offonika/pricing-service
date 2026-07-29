#!/usr/bin/env bash
set -uo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
RETRY_DELAY_SECONDS="${CUSTOMER_SETTLEMENTS_RETRY_DELAY_SECONDS:-600}"

cd "${REPO_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if "${PYTHON_BIN}" -m tasks.sync_customer_settlements; then
  exit 0
fi

sleep "${RETRY_DELAY_SECONDS}"
"${PYTHON_BIN}" -m tasks.sync_customer_settlements
