#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
LOCK_FILE="${ORDER_FULFILLMENT_BOT_LOCK_FILE:-/var/lock/order_fulfillment_bot_outbox.lock}"

cd "${REPO_DIR}"
if [[ -f "${REPO_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_DIR}/.env"
  set +a
fi

WORKER_TIMEOUT_SECONDS="${ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS:-600}"
if [[ ! "${WORKER_TIMEOUT_SECONDS}" =~ ^[0-9]*[1-9][0-9]*$ ]]; then
  echo "ORDER_FULFILLMENT_BOT_WORKER_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 64
fi

exec flock -n "${LOCK_FILE}" timeout \
  --signal=TERM \
  --kill-after=30s \
  "${WORKER_TIMEOUT_SECONDS}s" \
  "${PYTHON_BIN}" scripts/process_order_fulfillment_bot_outbox.py "$@"
