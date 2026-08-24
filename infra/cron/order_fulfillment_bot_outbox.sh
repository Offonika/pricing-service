#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
LOCK_FILE="${ORDER_FULFILLMENT_BOT_LOCK_FILE:-/var/lock/order_fulfillment_bot_outbox.lock}"

cd "${REPO_DIR}"
WORKER_TIMEOUT_SECONDS="${ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS:-}"
if [[ -z "${WORKER_TIMEOUT_SECONDS}" && -f "${REPO_DIR}/.env" ]]; then
  WORKER_TIMEOUT_SECONDS="$(
    sed -n 's/^ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS=//p' "${REPO_DIR}/.env" \
      | tail -n 1 \
      | tr -d '\r'
  )"
fi
WORKER_TIMEOUT_SECONDS="${WORKER_TIMEOUT_SECONDS:-600}"
if [[ ! "${WORKER_TIMEOUT_SECONDS}" =~ ^[0-9]*[1-9][0-9]*$ ]]; then
  echo "ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 64
fi

exec flock -n "${LOCK_FILE}" timeout \
  --signal=TERM \
  --kill-after=30s \
  "${WORKER_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" scripts/process_order_fulfillment_bot_outbox.py "$@"
