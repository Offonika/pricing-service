#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/onec_stock_availability.log"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
LOCK_FILE="${ONEC_STOCK_AVAILABILITY_LOCK_FILE:-/var/run/pricing_onec_stock_availability.lock}"
MODE="${1:-nightly}"

case "${MODE}" in
  nightly|weekly|backfill) ;;
  *)
    echo "unsupported mode: ${MODE}" >&2
    exit 2
    ;;
esac

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] stock availability sync is already running; skipping" \
    >> "${LOG_FILE}"
  exit 0
fi

echo "[$(date -Iseconds)] starting stock availability sync mode=${MODE}" >> "${LOG_FILE}"
"${PYTHON_BIN}" -m tasks.sync_onec_stock_availability --mode "${MODE}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] stock availability sync finished mode=${MODE}" >> "${LOG_FILE}"
