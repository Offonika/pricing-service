#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/staffing_sync.log"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

STAFF_FILE="${STAFFING_SYNC_STAFF_FILE:-}"
PLAN_FILE="${STAFFING_SYNC_PLAN_FILE:-}"
FACT_FILE="${STAFFING_SYNC_FACT_FILE:-}"
SNAPSHOT_DATES="${STAFFING_SYNC_SNAPSHOT_DATES:-}"
LOOKBACK_DAYS="${STAFFING_SYNC_LOOKBACK_DAYS:-1}"

mkdir -p "${LOG_DIR}"
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

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting staffing sync" >> "${LOG_FILE}"

if [[ -z "${STAFF_FILE}" || -z "${PLAN_FILE}" || -z "${FACT_FILE}" ]]; then
  echo "[$(date -Iseconds)] staffing sync skipped: STAFFING_SYNC_*_FILE is not fully configured" >> "${LOG_FILE}"
  exit 1
fi

for input_file in "${STAFF_FILE}" "${PLAN_FILE}" "${FACT_FILE}"; do
  if [[ ! -f "${input_file}" ]]; then
    echo "[$(date -Iseconds)] staffing sync failed: input file not found: ${input_file}" >> "${LOG_FILE}"
    exit 1
  fi
done

declare -a snapshot_args=()
if [[ -n "${SNAPSHOT_DATES}" ]]; then
  IFS=',' read -r -a raw_dates <<< "${SNAPSHOT_DATES}"
  for raw_date in "${raw_dates[@]}"; do
    snapshot_date="$(echo "${raw_date}" | xargs)"
    [[ -n "${snapshot_date}" ]] || continue
    snapshot_args+=(--snapshot-date "${snapshot_date}")
  done
else
  for days_ago in $(seq 0 "${LOOKBACK_DAYS}"); do
    snapshot_args+=(--snapshot-date "$(date -d "${days_ago} days ago" +%F)")
  done
fi

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${PYTHON_BIN}" -m tasks.sync_staffing \
  --staff-file "${STAFF_FILE}" \
  --plan-file "${PLAN_FILE}" \
  --fact-file "${FACT_FILE}" \
  "${snapshot_args[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
echo "[$(date -Iseconds)] finished staffing sync (status=${exit_code})" >> "${LOG_FILE}"

exit "${exit_code}"
