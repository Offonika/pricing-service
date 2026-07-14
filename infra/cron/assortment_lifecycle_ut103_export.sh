#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

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

LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/assortment_lifecycle_ut103_export.log}"
RUN_ID="$(date +%Y%m%d%H%M%S)"
MODE="${ASSORTMENT_LIFECYCLE_UT103_MODE:-apply}"
APPROVED_BY="${ASSORTMENT_LIFECYCLE_UT103_APPROVED_BY:-pricing-service-nightly}"
MESSAGE_ID="${ASSORTMENT_LIFECYCLE_UT103_MESSAGE_ID:-assortment-lifecycle-nightly-${RUN_ID}}"

mkdir -p "${LOG_DIR}"

cmd=(
  "${PYTHON_BIN}" -m tasks.refresh_assortment_lifecycle_classification
  --json
  --write-ready
  --allow-empty
  --export-mode "${MODE}"
  --approved-by "${APPROVED_BY}"
  --message-id "${MESSAGE_ID}"
)

if [[ -n "${ASSORTMENT_LIFECYCLE_UT103_EXCHANGE_ROOT:-}" ]]; then
  cmd+=(--exchange-root "${ASSORTMENT_LIFECYCLE_UT103_EXCHANGE_ROOT}")
fi

if [[ -n "${ASSORTMENT_LIFECYCLE_UT103_SOURCE:-}" ]]; then
  cmd+=(--source "${ASSORTMENT_LIFECYCLE_UT103_SOURCE}")
fi

if [[ "${ASSORTMENT_LIFECYCLE_UT103_OVERWRITE:-false}" == "true" ]]; then
  cmd+=(--overwrite)
fi

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting assortment lifecycle UT103 export mode=${MODE} message_id=${MESSAGE_ID}" \
  >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

set +e
"${cmd[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished assortment lifecycle UT103 export (status=${exit_code})" \
  >> "${LOG_FILE}"

exit "${exit_code}"
