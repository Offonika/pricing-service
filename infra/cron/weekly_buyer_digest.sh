#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/pricing-service"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="/var/log/pricing"
LOG_FILE="${LOG_DIR}/weekly_buyer_digest.log"
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
echo "[$timestamp] starting weekly buyer digest job" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${PYTHON_BIN}" -m tasks.generate_weekly_buyer_digest > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished weekly buyer digest job (status=${exit_code})" >> "${LOG_FILE}"

telegram_configured=false
if [[ -n "${WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_TOKEN:-}" \
  && -n "${WEEKLY_BUYER_DIGEST_ALERT_TELEGRAM_CHAT_ID:-}" ]]; then
  telegram_configured=true
elif [[ -n "${SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN:-}" \
  && -n "${SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID:-}" ]]; then
  telegram_configured=true
fi

if [[ "${telegram_configured}" == "true" ]]; then
  "${PYTHON_BIN}" infra/cron/weekly_buyer_digest_alert.py \
    --exit-code "${exit_code}" \
    --output-file "${tmp_output}" \
    || true
fi

exit "${exit_code}"
