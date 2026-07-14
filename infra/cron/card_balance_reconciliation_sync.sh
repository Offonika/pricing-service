#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/card_balance_reconciliation_sync.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_card_balance_reconciliation_sync.lock}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] card balance reconciliation sync skipped: another run is still active" >> "${LOG_FILE}"
  exit 0
fi

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting card balance reconciliation sync" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${PYTHON_BIN}" -m tasks.run_card_balance_reconciliation_sync > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished card balance reconciliation sync (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
