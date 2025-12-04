#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="/opt/pricing-service"
ENV_FILE="${REPO_DIR}/.env"
LOG_DIR="/var/log/pricing"
LOG_FILE="${LOG_DIR}/smartphone_releases.log"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # Экспортируем переменные окружения, чтобы cron видел тот же .env.
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting smartphone releases ingestion" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
python -m tasks.update_smartphone_releases > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished smartphone releases ingestion (status=${exit_code})" >> "${LOG_FILE}"

if [[ -n "${SMARTPHONE_RELEASES_ALERT_TELEGRAM_TOKEN:-}" && -n "${SMARTPHONE_RELEASES_ALERT_TELEGRAM_CHAT_ID:-}" ]]; then
  python infra/cron/telegram_alert.py \
    --exit-code "${exit_code}" \
    --output-file "${tmp_output}" \
    --job-name "Мониторинг релизов смартфонов" \
    || true
fi

exit "${exit_code}"

