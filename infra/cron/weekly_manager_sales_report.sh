#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/weekly_manager_sales_report.log"
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

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting weekly manager sales report job" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

telegram_configured=false
if [[ -n "${WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_TOKEN:-}" \
  && -n "${WEEKLY_MANAGER_SALES_REPORT_TELEGRAM_CHAT_ID:-}" ]]; then
  telegram_configured=true
elif [[ -n "${WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN:-}" \
  && -n "${WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID:-}" ]]; then
  telegram_configured=true
fi

cmd=("${PYTHON_BIN}" -m tasks.send_weekly_manager_sales_report)
if [[ -n "${WEEKLY_MANAGER_SALES_REPORT_DATE:-}" ]]; then
  cmd+=(--date "${WEEKLY_MANAGER_SALES_REPORT_DATE}")
fi
if [[ "${telegram_configured}" == "true" ]]; then
  cmd+=(--send-telegram)
fi

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${cmd[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished weekly manager sales report job (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
