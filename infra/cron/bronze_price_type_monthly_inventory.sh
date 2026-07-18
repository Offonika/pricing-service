#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/bronze_price_type_monthly_inventory.log"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"

mkdir -p "${LOG_DIR}"

# shellcheck source=/dev/null
if [[ -f "${ENV_LOADER}" ]]; then
    source "${ENV_LOADER}"
fi

cd "${REPO_DIR}"

{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] bronze monthly inventory: start"
    "${PYTHON_BIN}" scripts/build_bronze_monthly_inventory.py "$@"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] customer returns portrait: start"
    "${PYTHON_BIN}" scripts/build_customer_returns_portrait.py --days 90
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] bronze monthly inventory: done"
} >> "${LOG_FILE}" 2>&1
