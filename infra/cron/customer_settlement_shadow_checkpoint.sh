#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
ENV_FILE="${CUSTOMER_SETTLEMENTS_ENV_FILE:-${REPO_DIR}/.env}"
EXPECTED_DATABASE_NAME="${CUSTOMER_SETTLEMENTS_EXPECTED_DATABASE_NAME:?expected staging database is required}"
EXPECTED_PILOT_COUNT="${CUSTOMER_SETTLEMENTS_EXPECTED_PILOT_COUNT:-10}"

cd "${REPO_DIR}"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1091
  source "${REPO_DIR}/infra/cron/load_env.sh"
  load_env_file_preserve_json "${ENV_FILE}"
fi

"${PYTHON_BIN}" -m tasks.preflight_customer_settlement_shadow \
  --phase ready \
  --expected-pilot-count "${EXPECTED_PILOT_COUNT}" \
  --expected-database-name "${EXPECTED_DATABASE_NAME}"
"${PYTHON_BIN}" -m tasks.check_customer_settlement_health
