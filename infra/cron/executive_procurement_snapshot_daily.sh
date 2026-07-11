#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

RELEASE_DIR="${PRICING_SERVICE_ACTIVE_LINK:-/opt/MM/pricing-service-task43-current}"
STATE_REPO="${PRICING_SERVICE_STATE_REPO:-/opt/MM/pricing-service}"
PYTHON_BIN="${PRICING_SERVICE_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
ENV_FILE="${PRICING_SERVICE_ENV_FILE:-/opt/MM/pricing-service/.env}"
OUTPUT_FILE="${EXECUTIVE_PROCUREMENT_SNAPSHOT_OUTPUT:-${STATE_REPO}/build/executive_dashboard/procurement_open_orders_snapshot.json}"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/executive_procurement_snapshot.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_executive_procurement_snapshot.lock}"

mkdir -p "${LOG_DIR}" "$(dirname "${OUTPUT_FILE}")"
exec >>"${LOG_FILE}" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') start executive_procurement_snapshot ==="
(
  flock -n 9 || {
    echo "skip: previous run is still active"
    exit 0
  }

  cd "${RELEASE_DIR}"
  if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "error: python executable not found: ${PYTHON_BIN}"
    exit 1
  fi

  PYTHONPATH="${RELEASE_DIR}" "${PYTHON_BIN}" scripts/build_executive_procurement_snapshot.py \
    --env-file "${ENV_FILE}" \
    --output "${OUTPUT_FILE}" \
    --limit 5000

  echo "output: ${OUTPUT_FILE}"
) 9>"${LOCK_FILE}"
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') finish executive_procurement_snapshot ==="
