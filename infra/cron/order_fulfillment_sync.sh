#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/order_fulfillment_sync.log}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
MODE="${ORDER_FULFILLMENT_SYNC_MODE:-all}"
NEW_LIMIT="${ORDER_FULFILLMENT_SYNC_NEW_LIMIT:-500}"
SITE_CHAT_LIMIT="${ORDER_FULFILLMENT_SYNC_SITE_CHAT_LIMIT:-50}"
COURIER_CHAT_LIMIT="${ORDER_FULFILLMENT_SYNC_COURIER_CHAT_LIMIT:-10}"
REVIEW_LIMIT="${ORDER_FULFILLMENT_SYNC_REVIEW_LIMIT:-100}"
OUTPUT_DIR="${ORDER_FULFILLMENT_ARTIFACT_DIR:-${REPO_DIR}/.local/order-fulfillment-pilot}"

# Explicit cron overrides must win over defaults loaded from .env. Quick/chat
# runs set notifications to false and must stay silent even when the daily digest
# is enabled in the shared environment.
NOTIFY_ENABLED_OVERRIDE_SET="${ORDER_FULFILLMENT_NOTIFY_ENABLED+x}"
NOTIFY_ENABLED_OVERRIDE="${ORDER_FULFILLMENT_NOTIFY_ENABLED:-}"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if [[ -n "${NOTIFY_ENABLED_OVERRIDE_SET}" ]]; then
  export ORDER_FULFILLMENT_NOTIFY_ENABLED="${NOTIFY_ENABLED_OVERRIDE}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

cmd=(
  "${PYTHON_BIN}" infra/cron/order_fulfillment_sync.py
  --mode "${MODE}"
  --output-dir "${OUTPUT_DIR}"
  --new-limit "${NEW_LIMIT}"
  --site-chat-limit "${SITE_CHAT_LIMIT}"
  --courier-chat-limit "${COURIER_CHAT_LIMIT}"
  --review-limit "${REVIEW_LIMIT}"
)

if [[ "${ORDER_FULFILLMENT_SYNC_APPLY:-false}" == "true" ]]; then
  cmd+=(--apply)
fi

echo "[$(date -Iseconds)] starting order_fulfillment_sync mode=${MODE} apply=${ORDER_FULFILLMENT_SYNC_APPLY:-false}" >> "${LOG_FILE}"
"${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] order_fulfillment_sync finished" >> "${LOG_FILE}"
