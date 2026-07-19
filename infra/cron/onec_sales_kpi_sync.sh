#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/onec_sales_kpi_sync.log"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

# Re-sync a trailing window every night so late 1C fixes land: cost reversals for
# customer returns are posted days/weeks after the sale date (2026-07: rows synced
# once at D+2 kept stale costs and understated daily margin), so the window must
# cover the 1C cost-recalculation lag.
LOOKBACK_DAYS="${ONEC_SALES_KPI_LOOKBACK_DAYS:-35}"
BATCH_DAYS="${ONEC_SALES_KPI_BATCH_DAYS:-1}"
SLEEP_SECONDS="${ONEC_SALES_KPI_SLEEP_SECONDS:-0.5}"

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

today="$(date +%F)"
date_from="$(date -d "${LOOKBACK_DAYS} days ago" +%F)"

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting onec sales kpi sync date_from=${date_from} date_to=${today} batch_days=${BATCH_DAYS} sleep_seconds=${SLEEP_SECONDS}" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${PYTHON_BIN}" -m tasks.sync_onec_sales_kpi \
  --date-from "${date_from}" \
  --date-to "${today}" \
  --batch-days "${BATCH_DAYS}" \
  --sleep-seconds "${SLEEP_SECONDS}" > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished onec sales kpi sync (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
