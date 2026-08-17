#!/usr/bin/env bash
set -euo pipefail

export TZ="Europe/Moscow"
export PYTHONDONTWRITEBYTECODE=1
umask 027

RUNTIME_DIR="${MARGIN_FLOW_OBSERVER_RUNTIME_DIR:-/opt/MM/pricing-service-observers/margin-flow-pipeline-v1}"
SOURCE_ENV_FILE="${MARGIN_FLOW_OBSERVER_ENV_FILE:-/opt/MM/pricing-service/.env}"
PYTHON_BIN="${MARGIN_FLOW_OBSERVER_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
OUTPUT_ROOT="${MARGIN_FLOW_OBSERVER_OUTPUT_ROOT:-/opt/MM/pricing-service/reports/assortment_lifecycle/experiments/2026-08-17-margin-flow-pipeline-forward-observer-v1}"
LOG_DIR="${MARGIN_FLOW_OBSERVER_LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/margin_flow_pipeline_observer.log"
LOCK_FILE="${MARGIN_FLOW_OBSERVER_LOCK_FILE:-/var/lock/mm-margin-flow-pipeline-observer.lock}"
CONFIG_FILE="${RUNTIME_DIR}/margin-flow-pipeline-observer.json"
SCOPE_FILE="${RUNTIME_DIR}/scope.csv"
HASH_FILE="${RUNTIME_DIR}/runtime.sha256"
ENV_LOADER="${RUNTIME_DIR}/load_env.sh"

for required in \
  "${PYTHON_BIN}" \
  "${CONFIG_FILE}" \
  "${SCOPE_FILE}" \
  "${HASH_FILE}" \
  "${ENV_LOADER}"; do
  if [[ ! -e "${required}" ]]; then
    echo "margin-flow observer missing required runtime file: ${required}" >&2
    exit 2
  fi
done

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "margin-flow observer python is not executable: ${PYTHON_BIN}" >&2
  exit 2
fi

cd "${RUNTIME_DIR}"
if ! sha256sum --check --strict --quiet "${HASH_FILE}"; then
  echo "margin-flow observer runtime checksum validation failed" >&2
  exit 3
fi

# shellcheck disable=SC1090
source "${ENV_LOADER}"
load_env_file_preserve_json "${SOURCE_ENV_FILE}"
if [[ -z "${ONEC_DATABASE_URL:-}" ]]; then
  echo "margin-flow observer ONEC_DATABASE_URL is not configured" >&2
  exit 4
fi

mkdir -p "${LOG_DIR}" "${OUTPUT_ROOT}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] margin-flow observer skipped: another run is active" \
    >> "${LOG_FILE}"
  exit 0
fi

echo "[$(date -Iseconds)] margin-flow observer starting (read-only 1C, local append-only output)" \
  >> "${LOG_FILE}"
set +e
timeout --signal=TERM --kill-after=30s 10m \
  "${PYTHON_BIN}" -m tasks.observe_margin_flow_pipeline capture \
  --config "${CONFIG_FILE}" \
  --scope-csv "${SCOPE_FILE}" \
  --output-root "${OUTPUT_ROOT}" \
  --json >> "${LOG_FILE}" 2>&1
exit_code=$?
set -e
echo "[$(date -Iseconds)] margin-flow observer finished status=${exit_code}" >> "${LOG_FILE}"
exit "${exit_code}"
