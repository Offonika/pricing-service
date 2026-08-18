#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/assortment_lifecycle_classification.log"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"
# Ручные решения менеджера («Допродаём», «Не закупать» и другие) принимаются в
# приложении «Формирование заказа». Перед пересчётом выгружаем согласованные
# решения в файл ручных решений, иначе они не влияют на расчёт. Путь к файлу
# остаётся один — ASSORTMENT_MANUAL_OVERRIDES_JSON из .env.
SKIP_MANUAL_STATUS_EXPORT="${SKIP_MANUAL_STATUS_EXPORT:-0}"

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
echo "[$timestamp] starting assortment lifecycle classification refresh" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

if [[ "${SKIP_MANUAL_STATUS_EXPORT}" != "1" ]]; then
  set +e
  export_cmd=("${PYTHON_BIN}" -m tasks.export_manual_status_overrides --json)
  # Путь берём из той же переменной, что читает формула: иначе релизный запуск
  # записал бы решения в свой каталог, а расчёт читал бы файл рабочей копии.
  if [[ -n "${ASSORTMENT_MANUAL_OVERRIDES_JSON:-}" ]]; then
    export_cmd+=(--overrides-path "${ASSORTMENT_MANUAL_OVERRIDES_JSON}")
  fi
  "${export_cmd[@]}" > "${tmp_output}" 2>&1
  export_code=$?
  set -e
  cat "${tmp_output}" >> "${LOG_FILE}"
  if [[ ${export_code} -ne 0 ]]; then
    timestamp="$(date -Iseconds)"
    echo "[${timestamp}] manual status export failed (status=${export_code}); refresh skipped" \
      >> "${LOG_FILE}"
    exit ${export_code}
  fi
fi

set +e
"${PYTHON_BIN}" -m tasks.refresh_assortment_lifecycle_classification --json "$@" \
  > "${tmp_output}" 2>&1
exit_code=$?
set -e

cat "${tmp_output}" >> "${LOG_FILE}"
timestamp="$(date -Iseconds)"
echo "[$timestamp] finished assortment lifecycle classification refresh (status=${exit_code})" \
  >> "${LOG_FILE}"

exit "${exit_code}"
