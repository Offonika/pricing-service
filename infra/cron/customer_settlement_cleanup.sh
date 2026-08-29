#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
ENV_FILE="${CUSTOMER_SETTLEMENTS_ENV_FILE:-${REPO_DIR}/.env}"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
readonly REPO_DIR PYTHON_BIN ENV_FILE ENV_LOADER

cd "${REPO_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

: "${CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME:?expected database is required}"

JOB_TIMEOUT_SECONDS="${CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS:-120}"

timeout --signal=TERM --kill-after=5s "${JOB_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" -m tasks.cleanup_customer_settlements
