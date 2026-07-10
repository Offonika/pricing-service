#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_FILE:-${LOG_DIR}/sku_generation_ut103.log}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

MODE="${SKU_GENERATION_UT103_MODE:-apply}"
APPROVED_BY="${SKU_GENERATION_UT103_APPROVED_BY:-pricing-service-nightly}"
RUN_ID="$(date +%Y%m%d%H%M%S)"
MESSAGE_ID="${SKU_GENERATION_UT103_MESSAGE_ID:-sku-nightly-${RUN_ID}}"
SUBJECT_MESSAGE_ID="${SKU_GENERATION_UT103_SUBJECT_MESSAGE_ID:-missing-onec-subject-nightly-${RUN_ID}}"
CHANGED_AT="${SKU_GENERATION_UT103_CHANGED_AT:-$(date +%F)}"
SKU_PROPERTY_NAME="${SKU_GENERATION_UT103_PROPERTY_NAME:-SKU}"
SUBJECT_ENABLED="${SKU_GENERATION_UT103_SUBJECT_ENABLED:-true}"

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

cmd=(
  "${PYTHON_BIN}" -m tasks.generate_product_skus
  --write
  --export-existing
  --write-ready
  --allow-empty
  --mode "${MODE}"
  --approved-by "${APPROVED_BY}"
  --message-id "${MESSAGE_ID}"
  --changed-at "${CHANGED_AT}"
  --sku-property-name "${SKU_PROPERTY_NAME}"
)

if [[ -n "${SKU_GENERATION_UT103_EXCHANGE_ROOT:-}" ]]; then
  cmd+=(--exchange-root "${SKU_GENERATION_UT103_EXCHANGE_ROOT}")
fi

if [[ "${SKU_GENERATION_UT103_INCLUDE_INACTIVE:-false}" == "true" ]]; then
  cmd+=(--include-inactive)
fi

if [[ "${SKU_GENERATION_UT103_OVERWRITE:-false}" == "true" ]]; then
  cmd+=(--overwrite)
fi

echo "[$(date -Iseconds)] starting SKU generation UT103 export mode=${MODE} message_id=${MESSAGE_ID}" \
  >> "${LOG_FILE}"
"${cmd[@]}" >> "${LOG_FILE}" 2>&1
echo "[$(date -Iseconds)] finished SKU generation UT103 export message_id=${MESSAGE_ID}" \
  >> "${LOG_FILE}"

if [[ "${SUBJECT_ENABLED}" == "true" ]]; then
  subject_cmd=(
    "${PYTHON_BIN}" -m tasks.build_missing_onec_subject_updates
    --write-ready
    --allow-empty
    --json
    --mode "${MODE}"
    --approved-by "${APPROVED_BY}"
    --message-id "${SUBJECT_MESSAGE_ID}"
  )

  if [[ -n "${SKU_GENERATION_UT103_EXCHANGE_ROOT:-}" ]]; then
    subject_cmd+=(--exchange-root "${SKU_GENERATION_UT103_EXCHANGE_ROOT}")
  fi

  if [[ -n "${SKU_GENERATION_UT103_SUBJECT_LIMIT:-}" ]]; then
    subject_cmd+=(--limit "${SKU_GENERATION_UT103_SUBJECT_LIMIT}")
  fi

  if [[ "${SKU_GENERATION_UT103_SUBJECT_LLM:-false}" == "true" ]]; then
    subject_cmd+=(--llm)
    if [[ -n "${SKU_GENERATION_UT103_SUBJECT_LLM_LIMIT:-}" ]]; then
      subject_cmd+=(--llm-limit "${SKU_GENERATION_UT103_SUBJECT_LLM_LIMIT}")
    fi
  fi

  echo "[$(date -Iseconds)] starting missing 1C subject export mode=${MODE} message_id=${SUBJECT_MESSAGE_ID}" \
    >> "${LOG_FILE}"
  "${subject_cmd[@]}" >> "${LOG_FILE}" 2>&1
  echo "[$(date -Iseconds)] finished missing 1C subject export message_id=${SUBJECT_MESSAGE_ID}" \
    >> "${LOG_FILE}"
fi
