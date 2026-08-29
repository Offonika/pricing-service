#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
ENV_FILE="${CUSTOMER_SETTLEMENTS_ENV_FILE:-${REPO_DIR}/.env}"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
readonly REPO_DIR PYTHON_BIN ENV_FILE ENV_LOADER

cd "${REPO_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1091
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

EXPECTED_DATABASE_NAME="${CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME:?expected staging database is required}"
EXPECTED_PILOT_COUNT="${CUSTOMER_SETTLEMENTS_EXPECTED_PILOT_COUNT:-10}"
RECEIVABLE_ENV_FILE="${CUSTOMER_SETTLEMENTS_RECEIVABLE_ENV_FILE:?receivable env file is required}"
EXPECTED_RECEIVABLE_DATABASE_NAME="${CUSTOMER_SETTLEMENTS_RECEIVABLE_EXPECTED_DATABASE_NAME:?expected receivable database is required}"
JOB_TIMEOUT_SECONDS="${CUSTOMER_SETTLEMENTS_JOB_TIMEOUT_SECONDS:-90}"
readonly EXPECTED_DATABASE_NAME EXPECTED_PILOT_COUNT RECEIVABLE_ENV_FILE
readonly EXPECTED_RECEIVABLE_DATABASE_NAME JOB_TIMEOUT_SECONDS

timeout --signal=TERM --kill-after=5s "${JOB_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count "${EXPECTED_PILOT_COUNT}" \
  --expected-database-name "${EXPECTED_DATABASE_NAME}"
timeout --signal=TERM --kill-after=5s "${JOB_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" -m tasks.check_customer_settlement_receivable_drift \
  --receivable-env-file "${RECEIVABLE_ENV_FILE}" \
  --expected-receivable-database-name "${EXPECTED_RECEIVABLE_DATABASE_NAME}" \
  --expected-pilot-count "${EXPECTED_PILOT_COUNT}"
timeout --signal=TERM --kill-after=5s "${JOB_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" -m tasks.check_customer_settlement_health
