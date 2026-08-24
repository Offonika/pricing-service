#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/onec_assembly_crm_reconciler.log}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
SINCE_HOURS="${ONEC_ASSEMBLY_CRM_SINCE_HOURS:-4}"
LIMIT="${ONEC_ASSEMBLY_CRM_LIMIT:-300}"
APPLY="${ONEC_ASSEMBLY_CRM_APPLY:-false}"
LOCK_FILE="${ONEC_ASSEMBLY_CRM_LOCK_FILE:-/tmp/onec_assembly_crm_reconciler.lock}"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] onec_assembly_crm_reconciler skipped: previous run is still active" >> "${LOG_FILE}"
  exit 0
fi

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

if [[ -z "${MM_CRM_1C_ASSEMBLY_TOKEN:-}" && -z "${CRM_1C_ASSEMBLY_TOKEN:-}" ]]; then
  MM_CRM_1C_ASSEMBLY_TOKEN="$(
    ssh -o BatchMode=yes bitrix-box \
      'sudo -u mm php -r '\''$c=include "/var/www/mm/data/www/master-mobile.ru/local/php_interface/.mm_crm_1c_assembly_secret.php"; echo $c["token"];'\''' \
      2>/dev/null || true
  )"
  export MM_CRM_1C_ASSEMBLY_TOKEN
fi

cmd=(
  "${PYTHON_BIN}" -m tasks.reconcile_onec_assembly_to_crm
  --since-hours "${SINCE_HOURS}"
  --limit "${LIMIT}"
)

if [[ "${APPLY}" == "true" ]]; then
  cmd+=(--apply)
fi

echo "[$(date -Iseconds)] starting onec_assembly_crm_reconciler apply=${APPLY} since_hours=${SINCE_HOURS} limit=${LIMIT}" >> "${LOG_FILE}"
"${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] onec_assembly_crm_reconciler finished" >> "${LOG_FILE}"
