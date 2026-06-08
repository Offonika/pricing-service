#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
REPORT_DIR="${REPORT_DIR:-${REPO_DIR}/build/logs}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
run_id="$(date +%Y%m%d_%H%M%S)"

mkdir -p "${LOG_DIR}" "${REPORT_DIR}"
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

"${PYTHON_BIN}" -m tasks.competitor_matching_watchdog \
  --latest-report "${REPORT_DIR}/competitor_matching_nightly_latest.json" \
  --report-file "${REPORT_DIR}/competitor_matching_watchdog_${run_id}.json" \
  >> "${LOG_DIR}/competitor_matching_watchdog.log" 2>&1
