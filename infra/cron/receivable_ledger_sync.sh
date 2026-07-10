#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/receivable_ledger_sync.log"
PYTHON_BIN="${PYTHON_BIN:-${REPO_DIR}/.venv/bin/python}"
FALLBACK_PYTHON_BIN="${RECEIVABLE_LEDGER_FALLBACK_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
WINDOW_DAYS="${RECEIVABLE_LEDGER_WINDOW_DAYS:-7}"
OPENING_BALANCE_DATE="${RECEIVABLE_LEDGER_OPENING_BALANCE_DATE:-}"
OPENING_IMPORT_FILE="${RECEIVABLE_LEDGER_OPENING_IMPORT_FILE:-}"
OPENING_LAYERS="${RECEIVABLE_LEDGER_OPENING_LAYERS:-regular_opening,employee_opening}"
DAILY_LAYERS="${RECEIVABLE_DAILY_LAYERS:-}"
WORKPLACE_CACHE_ENABLED="${RECEIVABLE_WORKPLACE_CACHE_REBUILD_ENABLED:-1}"
WORKPLACE_CACHE_REQUIRED="${RECEIVABLE_WORKPLACE_CACHE_REQUIRED:-0}"
WORKPLACE_CACHE_INCLUDE_ONEC_OPEN_DEBT="${RECEIVABLE_WORKPLACE_CACHE_INCLUDE_ONEC_OPEN_DEBT:-1}"

mkdir -p "${LOG_DIR}"
cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_LOADER}"
  load_env_file_preserve_json "${ENV_FILE}"
fi

if [[ ! -x "${PYTHON_BIN}" && -x "${FALLBACK_PYTHON_BIN}" ]]; then
  PYTHON_BIN="${FALLBACK_PYTHON_BIN}"
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
  fi
fi

today="$(date +%F)"
yesterday="$(date -d 'yesterday' +%F)"

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting receivable ledger sync snapshots=${yesterday},${today} lookback_days=${WINDOW_DAYS}" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"
if [[ -n "${OPENING_BALANCE_DATE}" ]]; then
  echo "[$timestamp] opening_balance_date=${OPENING_BALANCE_DATE}" >> "${LOG_FILE}"
fi
if [[ -n "${OPENING_IMPORT_FILE}" ]]; then
  echo "[$timestamp] legacy opening_import_file is configured but ignored in 1C-only mode: ${OPENING_IMPORT_FILE}" >> "${LOG_FILE}"
fi
for legacy_env_name in \
  RECEIVABLE_CURRENT_IMPORT_FILE \
  RECEIVABLE_CURRENT_IMPORT_FILE_TODAY \
  RECEIVABLE_CURRENT_IMPORT_FILE_YESTERDAY \
  RECEIVABLE_EMPLOYEE_CURRENT_IMPORT_FILE \
  RECEIVABLE_EMPLOYEE_CURRENT_IMPORT_FILE_TODAY \
  RECEIVABLE_EMPLOYEE_CURRENT_IMPORT_FILE_YESTERDAY
do
  legacy_env_value="${!legacy_env_name:-}"
  if [[ -n "${legacy_env_value}" ]]; then
    echo "[$timestamp] legacy ${legacy_env_name} is configured but ignored in 1C-only mode: ${legacy_env_value}" >> "${LOG_FILE}"
  fi
done
echo "[$timestamp] opening_layers=${OPENING_LAYERS}" >> "${LOG_FILE}"
if [[ -n "${DAILY_LAYERS}" ]]; then
  echo "[$timestamp] daily_layers=${DAILY_LAYERS}" >> "${LOG_FILE}"
else
  echo "[$timestamp] daily layers disabled: set RECEIVABLE_DAILY_LAYERS only after 1C extractor acceptance" >> "${LOG_FILE}"
fi

run_step() {
  local step_name="$1"
  shift
  local tmp_output
  tmp_output="$(mktemp)"

  set +e
  "$@" > "${tmp_output}" 2>&1
  local exit_code=$?
  set -e

  cat "${tmp_output}" >> "${LOG_FILE}"
  rm -f "${tmp_output}"

  if [[ ${exit_code} -ne 0 ]]; then
    echo "[$(date -Iseconds)] receivable ledger step failed: ${step_name} (status=${exit_code})" >> "${LOG_FILE}"
    return "${exit_code}"
  fi

  echo "[$(date -Iseconds)] receivable ledger step finished: ${step_name}" >> "${LOG_FILE}"
}

run_optional_step() {
  local step_name="$1"
  shift
  local tmp_output
  tmp_output="$(mktemp)"

  set +e
  "$@" > "${tmp_output}" 2>&1
  local exit_code=$?
  set -e

  cat "${tmp_output}" >> "${LOG_FILE}"
  rm -f "${tmp_output}"

  if [[ ${exit_code} -ne 0 ]]; then
    echo "[$(date -Iseconds)] optional receivable ledger step failed: ${step_name} (status=${exit_code})" >> "${LOG_FILE}"
    if [[ "${WORKPLACE_CACHE_REQUIRED}" == "1" ]]; then
      return "${exit_code}"
    fi
    return 0
  fi

  echo "[$(date -Iseconds)] optional receivable ledger step finished: ${step_name}" >> "${LOG_FILE}"
}

if [[ -n "${OPENING_BALANCE_DATE}" || -n "${OPENING_IMPORT_FILE}" ]]; then
  opening_cmd=("${PYTHON_BIN}" -m tasks.sync_receivable_opening)
  if [[ -n "${OPENING_BALANCE_DATE}" ]]; then
    opening_cmd+=(--opening-balance-date "${OPENING_BALANCE_DATE}")
  fi
  IFS=',' read -r -a opening_layers_array <<< "${OPENING_LAYERS}"
  for layer_name in "${opening_layers_array[@]}"; do
    if [[ -n "${layer_name}" ]]; then
      opening_cmd+=(--layer "${layer_name}")
    fi
  done
  run_step "opening" "${opening_cmd[@]}"
fi

for snapshot_date in "${yesterday}" "${today}"; do
  if [[ -n "${DAILY_LAYERS}" ]]; then
    daily_cmd=(
      "${PYTHON_BIN}" -m tasks.sync_receivable_daily_events
      --snapshot-date "${snapshot_date}"
      --window-days "${WINDOW_DAYS}"
    )
    IFS=',' read -r -a daily_layers_array <<< "${DAILY_LAYERS}"
    for layer_name in "${daily_layers_array[@]}"; do
      if [[ -n "${layer_name}" ]]; then
        daily_cmd+=(--layer "${layer_name}")
      fi
    done
    run_step \
      "daily_events:${snapshot_date}" \
      "${daily_cmd[@]}"
  else
    echo "[$(date -Iseconds)] skip daily_events:${snapshot_date}; enrichment layers disabled" >> "${LOG_FILE}"
  fi

  rebuild_cmd=(
    "${PYTHON_BIN}" -m tasks.rebuild_receivable_read_models
    --snapshot-date "${snapshot_date}"
  )
  run_step \
    "read_models:${snapshot_date}" \
    "${rebuild_cmd[@]}"

  if [[ "${WORKPLACE_CACHE_ENABLED}" == "1" ]]; then
    cache_cmd=(
      "${PYTHON_BIN}" -m tasks.rebuild_receivable_workplace_cache
      --date "${snapshot_date}"
    )
    if [[ "${WORKPLACE_CACHE_INCLUDE_ONEC_OPEN_DEBT}" == "1" ]]; then
      cache_cmd+=(--include-onec-open-debt)
    fi
    run_optional_step \
      "workplace_cache:${snapshot_date}" \
      "${cache_cmd[@]}"
  else
    echo "[$(date -Iseconds)] skip workplace_cache:${snapshot_date}; disabled by RECEIVABLE_WORKPLACE_CACHE_REBUILD_ENABLED" >> "${LOG_FILE}"
  fi
done

echo "[$(date -Iseconds)] receivable ledger sync job finished successfully" >> "${LOG_FILE}"
