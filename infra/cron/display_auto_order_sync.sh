#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

RUN_MODE="shadow"
PRINT_RUN_MODE="false"
MODE_ARGUMENT_COUNT=0
for argument in "$@"; do
  case "${argument}" in
    --shadow)
      RUN_MODE="shadow"
      MODE_ARGUMENT_COUNT=$((MODE_ARGUMENT_COUNT + 1))
      ;;
    --persist-internal-drafts)
      RUN_MODE="persist_internal_drafts"
      MODE_ARGUMENT_COUNT=$((MODE_ARGUMENT_COUNT + 1))
      ;;
    --print-run-mode)
      PRINT_RUN_MODE="true"
      ;;
    --help|-h)
      echo "Usage: $0 [--shadow|--persist-internal-drafts] [--print-run-mode]"
      exit 0
      ;;
    *)
      echo "Unknown argument: ${argument}" >&2
      exit 2
      ;;
  esac
done
if [[ "${MODE_ARGUMENT_COUNT}" -gt 1 ]]; then
  echo "Choose exactly one run mode: --shadow or --persist-internal-drafts" >&2
  exit 2
fi

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/display_auto_order_sync.log"
LOCK_FILE="${LOCK_FILE:-/tmp/pricing_display_auto_order_sync.lock}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

mkdir -p "${LOG_DIR}" "${REPO_DIR}/build/bitrix"
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

FOLDER="${DISPLAY_AUTO_ORDER_FOLDER:-дисплеи}"
AS_OF="${DISPLAY_AUTO_ORDER_AS_OF:-$(date +%F)}"
AUTO_ORDER_POLICY_JSON="${DISPLAY_AUTO_ORDER_POLICY_JSON:-${REPO_DIR}/config/assortment/display-auto-order-policy.json}"
TARGET_DAYS="${DISPLAY_AUTO_ORDER_TARGET_DAYS:-}"
ORDER_CADENCE_DAYS="${DISPLAY_AUTO_ORDER_ORDER_CADENCE_DAYS:-}"
SUPPLIER_PREPARE_DAYS="${DISPLAY_AUTO_ORDER_SUPPLIER_PREPARE_DAYS:-${DISPLAY_AUTO_ORDER_SUPPLIER_ASSEMBLY_DAYS:-}}"
LOGISTICS_DAYS="${DISPLAY_AUTO_ORDER_LOGISTICS_DAYS:-${DISPLAY_AUTO_ORDER_DELIVERY_DAYS:-}}"
SUPPLIER_DELAY_BUFFER_DAYS="${DISPLAY_AUTO_ORDER_SUPPLIER_DELAY_BUFFER_DAYS:-}"
RECEIVING_BUFFER_DAYS="${DISPLAY_AUTO_ORDER_RECEIVING_BUFFER_DAYS:-}"
SAFETY_STOCK_DAYS="${DISPLAY_AUTO_ORDER_SAFETY_STOCK_DAYS:-}"
MIN_DISPLAY_QTY="${DISPLAY_AUTO_ORDER_MIN_DISPLAY_QTY:-}"
MIN_ORDER_QTY="${DISPLAY_AUTO_ORDER_MIN_ORDER_QTY:-}"
MAX_ORDER_QTY="${DISPLAY_AUTO_ORDER_MAX_ORDER_QTY:-}"
ASSIGNED_BY_ID="${DISPLAY_AUTO_ORDER_ASSIGNED_BY_ID:-}"
APPLY="${DISPLAY_AUTO_ORDER_APPLY:-false}"
REPORT_DIR="${DISPLAY_AUTO_ORDER_REPORT_DIR:-${REPO_DIR}/reports/assortment_lifecycle/${AS_OF}}"
OUTPUT_CSV="${DISPLAY_AUTO_ORDER_OUTPUT_CSV:-${REPORT_DIR}/display-auto-order-dry-run.csv}"
OUTPUT_JSON="${DISPLAY_AUTO_ORDER_OUTPUT_JSON:-${REPORT_DIR}/display-auto-order-dry-run-summary.json}"
ORDER_FORMATION_OUTPUT_JSON="${DISPLAY_AUTO_ORDER_FORMATION_OUTPUT_JSON:-${REPORT_DIR}/procurement-order-formation-dry-run.json}"
ORDER_FORMATION_OUTPUT_CSV="${DISPLAY_AUTO_ORDER_FORMATION_OUTPUT_CSV:-${REPORT_DIR}/procurement-order-formation-lines-dry-run.csv}"
CONFIGURED_ORDER_FORMATION_PERSIST_DB="${DISPLAY_AUTO_ORDER_FORMATION_PERSIST_DB:-false}"
ORDER_FORMATION_PERSIST_DB="false"
USE_ADAPTIVE_LEAD_TIME="${DISPLAY_AUTO_ORDER_USE_ADAPTIVE_LEAD_TIME:-true}"
ADAPTIVE_REQUIRED="${DISPLAY_AUTO_ORDER_ADAPTIVE_REQUIRED:-true}"
LEAD_TIME_CSV="${DISPLAY_AUTO_ORDER_LEAD_TIME_CSV:-${REPORT_DIR}/display-supplier-lead-time-history.csv}"
SEASONALITY_CSV="${DISPLAY_AUTO_ORDER_SEASONALITY_CSV:-${REPORT_DIR}/display-supplier-lead-time-seasonality.csv}"
ADAPTIVE_OUTPUT_CSV="${DISPLAY_AUTO_ORDER_ADAPTIVE_OUTPUT_CSV:-${REPORT_DIR}/display-auto-order-adaptive-lead-time-comparison.csv}"
ADAPTIVE_OUTPUT_JSON="${DISPLAY_AUTO_ORDER_ADAPTIVE_OUTPUT_JSON:-${REPORT_DIR}/display-auto-order-adaptive-lead-time-comparison-summary.json}"
ADAPTIVE_SYNC_READY_CSV="${DISPLAY_AUTO_ORDER_ADAPTIVE_SYNC_READY_CSV:-${REPORT_DIR}/display-auto-order-adaptive-sync-ready.csv}"

is_truthy() {
  case "${1,,}" in
    true|1|yes|y|да)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

FORMATION_MODE_ARGS=(--shadow)
if [[ "${RUN_MODE}" == "persist_internal_drafts" ]]; then
  if ! is_truthy "${CONFIGURED_ORDER_FORMATION_PERSIST_DB}"; then
    echo "persist_internal_drafts requires DISPLAY_AUTO_ORDER_FORMATION_PERSIST_DB=true" >&2
    exit 2
  fi
  ORDER_FORMATION_PERSIST_DB="true"
  FORMATION_MODE_ARGS=(--persist-db --supersede-open-batches)
fi

if is_truthy "${PRINT_RUN_MODE}"; then
  printf '{"run_mode":"%s","configured_persist_db":"%s","effective_persist_db":"%s","formation_args":"%s"}\n' \
    "${RUN_MODE}" \
    "${CONFIGURED_ORDER_FORMATION_PERSIST_DB}" \
    "${ORDER_FORMATION_PERSIST_DB}" \
    "${FORMATION_MODE_ARGS[*]}"
  exit 0
fi

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] display auto-order sync skipped: another run is still active" >> "${LOG_FILE}"
  exit 0
fi

tmp_output="$(mktemp)"
cleanup() {
  rm -f "${tmp_output}"
}
trap cleanup EXIT

timestamp="$(date -Iseconds)"
echo "[$timestamp] starting display auto-order sync mode=${RUN_MODE} folder=${FOLDER} as_of=${AS_OF} policy=${AUTO_ORDER_POLICY_JSON} target=${TARGET_DAYS:-policy} cadence=${ORDER_CADENCE_DAYS:-policy} prepare=${SUPPLIER_PREPARE_DAYS:-policy} logistics=${LOGISTICS_DAYS:-policy} max_qty=${MAX_ORDER_QTY:-policy} assigned=${ASSIGNED_BY_ID:-shared_queue} legacy_apply=${APPLY} formation_persist_db=${ORDER_FORMATION_PERSIST_DB} adaptive=${USE_ADAPTIVE_LEAD_TIME}" >> "${LOG_FILE}"
echo "[$timestamp] python interpreter: ${PYTHON_BIN}" >> "${LOG_FILE}"

mkdir -p "${REPORT_DIR}"

build_cmd=(
  "${PYTHON_BIN}"
  -m tasks.build_display_auto_order_dry_run
  --folder "${FOLDER}"
  --auto-order-policy-json "${AUTO_ORDER_POLICY_JSON}"
  --as-of "${AS_OF}"
  --output-csv "${OUTPUT_CSV}"
  --output-json "${OUTPUT_JSON}"
  --use-active-display-family-registry
  --json
)
if [[ -n "${TARGET_DAYS}" ]]; then
  build_cmd+=(--target-days "${TARGET_DAYS}")
fi
if [[ -n "${ORDER_CADENCE_DAYS}" ]]; then
  build_cmd+=(--order-cadence-days "${ORDER_CADENCE_DAYS}")
fi
if [[ -n "${SUPPLIER_PREPARE_DAYS}" ]]; then
  build_cmd+=(--supplier-prepare-days "${SUPPLIER_PREPARE_DAYS}")
fi
if [[ -n "${LOGISTICS_DAYS}" ]]; then
  build_cmd+=(--logistics-days "${LOGISTICS_DAYS}")
fi
if [[ -n "${SUPPLIER_DELAY_BUFFER_DAYS}" ]]; then
  build_cmd+=(--supplier-delay-buffer-days "${SUPPLIER_DELAY_BUFFER_DAYS}")
fi
if [[ -n "${RECEIVING_BUFFER_DAYS}" ]]; then
  build_cmd+=(--receiving-buffer-days "${RECEIVING_BUFFER_DAYS}")
fi
if [[ -n "${SAFETY_STOCK_DAYS}" ]]; then
  build_cmd+=(--safety-stock-days "${SAFETY_STOCK_DAYS}")
fi
if [[ -n "${MIN_DISPLAY_QTY}" ]]; then
  build_cmd+=(--min-display-qty "${MIN_DISPLAY_QTY}")
fi
if [[ -n "${MIN_ORDER_QTY}" ]]; then
  build_cmd+=(--min-order-qty "${MIN_ORDER_QTY}")
fi
if [[ -n "${MAX_ORDER_QTY}" ]]; then
  build_cmd+=(--max-order-qty "${MAX_ORDER_QTY}")
fi

set +e
"${build_cmd[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e
cat "${tmp_output}" >> "${LOG_FILE}"
echo "[$(date -Iseconds)] finished display auto-order dry-run build (status=${exit_code})" >> "${LOG_FILE}"
if [[ "${exit_code}" -ne 0 ]]; then
  exit "${exit_code}"
fi

SYNC_INPUT_CSV="${OUTPUT_CSV}"
if is_truthy "${USE_ADAPTIVE_LEAD_TIME}"; then
  if [[ ! -f "${LEAD_TIME_CSV}" ]]; then
    echo "[$(date -Iseconds)] adaptive lead-time input is missing: ${LEAD_TIME_CSV}" >> "${LOG_FILE}"
    if is_truthy "${ADAPTIVE_REQUIRED}"; then
      exit 1
    fi
  else
    adaptive_cmd=(
      "${PYTHON_BIN}"
      -m tasks.report_display_auto_order_adaptive_lead_time_comparison
      --dry-run-csv "${OUTPUT_CSV}"
      --lead-time-csv "${LEAD_TIME_CSV}"
      --auto-order-policy-json "${AUTO_ORDER_POLICY_JSON}"
      --as-of "${AS_OF}"
      --output-csv "${ADAPTIVE_OUTPUT_CSV}"
      --output-json "${ADAPTIVE_OUTPUT_JSON}"
      --sync-ready-csv "${ADAPTIVE_SYNC_READY_CSV}"
      --use-active-display-family-registry
      --json
    )
    if [[ -f "${SEASONALITY_CSV}" ]]; then
      adaptive_cmd+=(--seasonality-csv "${SEASONALITY_CSV}")
    else
      echo "[$(date -Iseconds)] adaptive seasonality input is missing, continuing without it: ${SEASONALITY_CSV}" >> "${LOG_FILE}"
    fi

    set +e
    "${adaptive_cmd[@]}" > "${tmp_output}" 2>&1
    exit_code=$?
    set -e
    cat "${tmp_output}" >> "${LOG_FILE}"
    echo "[$(date -Iseconds)] finished adaptive lead-time comparison (status=${exit_code})" >> "${LOG_FILE}"
    if [[ "${exit_code}" -ne 0 ]]; then
      exit "${exit_code}"
    fi
    SYNC_INPUT_CSV="${ADAPTIVE_SYNC_READY_CSV}"
  fi
fi

formation_cmd=(
  "${PYTHON_BIN}"
  -m tasks.build_procurement_order_formation_dry_run
  --input-csv "${SYNC_INPUT_CSV}"
  --lead-time-csv "${LEAD_TIME_CSV}"
  --source-summary-json "${OUTPUT_JSON}"
  --batch-id "${AS_OF}"
  --order-date "${AS_OF}"
  --output-json "${ORDER_FORMATION_OUTPUT_JSON}"
  --output-csv "${ORDER_FORMATION_OUTPUT_CSV}"
  --json
)
formation_cmd+=("${FORMATION_MODE_ARGS[@]}")
if [[ -n "${ASSIGNED_BY_ID}" ]]; then
  formation_cmd+=(--responsible-bitrix-user-id "${ASSIGNED_BY_ID}")
fi

set +e
"${formation_cmd[@]}" > "${tmp_output}" 2>&1
exit_code=$?
set -e
cat "${tmp_output}" >> "${LOG_FILE}"

timestamp="$(date -Iseconds)"
echo "[$timestamp] finished order-formation sync (status=${exit_code}, persist_db=${ORDER_FORMATION_PERSIST_DB})" >> "${LOG_FILE}"

exit "${exit_code}"
