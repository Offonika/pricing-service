#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${PRICING_SERVICE_ROOT:-/opt/MM/pricing-service}"
PYTHON_BIN="${PYTHON_BIN:-${ROOT_DIR}/.venv/bin/python}"
ENV_FILE="${PRICING_ENV_FILE:-${ROOT_DIR}/.env}"
STATE_PATH="${MANUAL_MATCHING_TASK_STATE_PATH:-/var/lib/pricing/manual_matching_bitrix_tasks_state.json}"

cd "${ROOT_DIR}"

if [[ -f "${ROOT_DIR}/infra/cron/load_env.sh" ]]; then
  # shellcheck source=/opt/MM/pricing-service/infra/cron/load_env.sh
  source "${ROOT_DIR}/infra/cron/load_env.sh"
  load_env_file_preserve_json "${ENV_FILE}"
fi

exec "${PYTHON_BIN}" -m tasks.manual_matching_bitrix_tasks \
  --apply \
  --state-path "${STATE_PATH}" \
  --json
