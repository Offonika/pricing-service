#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/sku_result_sync_ut103.log}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

cmd=(
  "${PYTHON_BIN}" -m tasks.apply_ut103_sku_results
  --json
)

if [[ -n "${SKU_RESULT_SYNC_UT103_EXCHANGE_ROOT:-}" ]]; then
  cmd+=(--exchange-root "${SKU_RESULT_SYNC_UT103_EXCHANGE_ROOT}")
fi

if [[ -n "${SKU_RESULT_SYNC_UT103_PROPERTY_NAME:-}" ]]; then
  cmd+=(--property-name "${SKU_RESULT_SYNC_UT103_PROPERTY_NAME}")
fi

if [[ "${SKU_RESULT_SYNC_UT103_DRY_RUN:-false}" == "true" ]]; then
  cmd+=(--dry-run)
fi

echo "[$(date -Iseconds)] starting SKU result sync from UT103" >> "${LOG_FILE}"
"${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] finished SKU result sync from UT103" >> "${LOG_FILE}"
