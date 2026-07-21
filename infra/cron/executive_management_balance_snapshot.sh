#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/executive_management_balance_snapshot.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_executive_management_balance_snapshot.lock}"
PYTHON_BIN="${PRICING_SERVICE_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] management balance snapshot skipped: another run is active" >> "${LOG_FILE}"
  exit 0
fi

echo "[$(date -Iseconds)] starting operational management balance snapshot" >> "${LOG_FILE}"
/opt/MM/mm-compensation/infra/cron/employee_payroll_balance_snapshot_daily.sh
"${PYTHON_BIN}" -m tasks.sync_executive_service_accruals >> "${LOG_FILE}" 2>&1
"${PYTHON_BIN}" -m tasks.build_executive_management_balance_snapshot \
  --view operational --trigger cron >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] finished operational management balance snapshot" >> "${LOG_FILE}"
