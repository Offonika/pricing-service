#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/procurement_order_registry_sync.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_procurement_order_registry_sync.lock}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
RESULT_PATH="${RESULT_PATH:-${REPO_DIR}/build/bitrix/procurement_order_registry_sync.json}"

mkdir -p "${LOG_DIR}" "${REPO_DIR}/build/bitrix"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] unified procurement registry sync skipped: already running" >> "${LOG_FILE}"
  exit 0
fi

echo "[$(date -Iseconds)] starting unified procurement registry sync" >> "${LOG_FILE}"
set +e
"${PYTHON_BIN}" -m tasks.sync_procurement_order_registry \
  --contours ordinary,cargo,ved_import \
  --limit "${PROCUREMENT_SYNC_LIMIT:-5000}" \
  --assigned-by-id "${PROCUREMENT_SYNC_ASSIGNED_BY_ID:-130750}" \
  --finance-user-id "${PROCUREMENT_CARGO_FINANCE_USER_ID:-}" \
  --result-path "${RESULT_PATH}" \
  --apply \
  --sync-bitrix >> "${LOG_FILE}" 2>&1
status=$?
set -e
echo "[$(date -Iseconds)] finished unified procurement registry sync (status=${status})" >> "${LOG_FILE}"
exit "${status}"
