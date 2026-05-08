#!/usr/bin/env bash
set -euo pipefail

OPENCLAW_ROOT="${OPENCLAW_ROOT:-/home/deploy/.openclaw}"
OPENCLAW_ENV_FILE="${OPENCLAW_ENV_FILE:-${OPENCLAW_ROOT}/.env}"
SCRIPT_PATH="${SCRIPT_PATH:-${OPENCLAW_ROOT}/workspace/scripts/weekly_manager_sales_reports_from_a.py}"

export OPENCLAW_ENV_FILE

exec /usr/bin/env python3 "${SCRIPT_PATH}" "$@"
