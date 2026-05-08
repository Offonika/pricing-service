#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
LOG_DIR="${LOG_DIR:-/var/log/pricing}"
LOG_FILE="${LOG_DIR}/competitor_matching_nightly.log"
REPORT_DIR="${REPORT_DIR:-${REPO_DIR}/build/logs}"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

LOOKBACK_DAYS="${COMPETITOR_MATCHING_LOOKBACK_DAYS:-3}"
EMBED_MODEL="${COMPETITOR_MATCHING_EMBED_MODEL:-text-embedding-3-small}"
AUTO_ACCEPT_MIN_SCORE="${COMPETITOR_MATCHING_AUTO_ACCEPT_MIN_SCORE:-0.80}"
LIVE_CACHE_LIMIT="${COMPETITOR_MATCHING_LIVE_CACHE_LIMIT:-3000}"
LIVE_CACHE_MAX_SECONDS="${COMPETITOR_MATCHING_LIVE_CACHE_MAX_SECONDS:-7200}"

mkdir -p "${LOG_DIR}" "${REPORT_DIR}"
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

first_seen_after="${COMPETITOR_MATCHING_FIRST_SEEN_AFTER:-$(date -d "${LOOKBACK_DAYS} days ago" +%F)}"
run_id="$(date +%Y%m%d_%H%M%S)"

timestamp="$(date -Iseconds)"
{
  echo "[$timestamp] starting competitor matching nightly"
  echo "[$timestamp] lookback_days=${LOOKBACK_DAYS} first_seen_after=${first_seen_after} embed_model=${EMBED_MODEL}"
  echo "[$timestamp] python interpreter: ${PYTHON_BIN}"
} >> "${LOG_FILE}"

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
    echo "[$(date -Iseconds)] competitor step failed: ${step_name} (status=${exit_code})" >> "${LOG_FILE}"
    return "${exit_code}"
  fi

  echo "[$(date -Iseconds)] competitor step finished: ${step_name}" >> "${LOG_FILE}"
}

product_refresh_ok=1
if run_step "import_topcontrol_products_db" \
  "${PYTHON_BIN}" -m tasks.import_topcontrol_products_db; then
  run_step "product_phone_model_backfill" \
    "${PYTHON_BIN}" -m tasks.backfill_phone_model_links \
      --products \
      --batch-size 1000 \
      --progress-every 5000
else
  product_refresh_ok=0
  echo "[$(date -Iseconds)] product refresh failed; FTP catalog refresh will continue, matching will be skipped" >> "${LOG_FILE}"
fi

run_step "import_competitor_ftp" \
  "${PYTHON_BIN}" -m tasks.import_competitor_ftp

run_step "match_competitor_ftp" \
  "${PYTHON_BIN}" -m tasks.match_competitor_ftp \
    --days-back "${LOOKBACK_DAYS}" \
    --disable-category-llm \
    --skip-display-attrs

run_step "normalize_competitor_item_type" \
  "${PYTHON_BIN}" -m tasks.normalize_competitor_item_type \
    --missing-only

run_step "extract_competitor_attrs_parser_only" \
  "${PYTHON_BIN}" -m tasks.extract_competitor_attrs \
    --parser-only \
    --first-seen-after "${first_seen_after}" \
    --only-null \
    --parse-version parser_v1

if [[ "${product_refresh_ok}" -eq 1 ]]; then
  run_step "match_competitor_items_compatibility" \
    "${PYTHON_BIN}" -m tasks.match_competitor_items \
      --first-seen-after "${first_seen_after}" \
      --only-missing-compat \
      --batch-size 1000

  run_step "competitor_phone_model_backfill" \
    "${PYTHON_BIN}" -m tasks.backfill_phone_model_links \
      --competitors \
      --first-seen-after "${first_seen_after}" \
      --batch-size 1000 \
      --progress-every 1000

  run_step "compute_embeddings" \
    "${PYTHON_BIN}" -m tasks.compute_embeddings \
      --target both \
      --only-changed \
      --embed-model "${EMBED_MODEL}"

  run_step "match_competitor_items_embeddings" \
    "${PYTHON_BIN}" -m tasks.match_competitor_items_embeddings \
      --only-null \
      --first-seen-after "${first_seen_after}" \
      --auto-accept-min-score "${AUTO_ACCEPT_MIN_SCORE}" \
      --no-auto-accept-unique \
      --report-file "${REPORT_DIR}/match_competitor_items_embeddings_${run_id}.json" \
      --report-csv "${REPORT_DIR}/match_competitor_items_embeddings_${run_id}.csv"

  run_step "review_unsafe_competitor_matches" \
    "${PYTHON_BIN}" -m tasks.review_unsafe_competitor_matches \
      --first-seen-after "2026-05-01" \
      --report-file "${REPORT_DIR}/unsafe_competitor_matches_${run_id}.json"

  run_step "report_competitor_items_needs_compat_review" \
    "${PYTHON_BIN}" -m tasks.report_competitor_items_needs_compat_review \
      --first-seen-after "${first_seen_after}" \
      --report-file "${REPORT_DIR}/competitor_items_needs_compat_review_${run_id}.json" \
      --report-csv "${REPORT_DIR}/competitor_items_needs_compat_review_${run_id}.csv"

  run_step "export_competitor_match_review_queue" \
    "${PYTHON_BIN}" -m tasks.export_competitor_match_review_queue \
      --first-seen-after "${first_seen_after}" \
      --report-file "${REPORT_DIR}/competitor_match_review_queue_${run_id}.json" \
      --report-csv "${REPORT_DIR}/competitor_match_review_queue_${run_id}.csv"

  run_step "refresh_live_candidate_cache" \
    "${PYTHON_BIN}" -m tasks.refresh_live_candidate_cache \
      --limit "${LIVE_CACHE_LIMIT}" \
      --max-seconds "${LIVE_CACHE_MAX_SECONDS}" \
      --batch-size 500 \
      --progress-every 1000 \
      --report-file "${REPORT_DIR}/live_candidate_cache_${run_id}.json"
else
  echo "[$(date -Iseconds)] skipping compatibility/embeddings/matching because product refresh failed" >> "${LOG_FILE}"
fi

run_step "report_competitor_matching_quality" \
  "${PYTHON_BIN}" -m tasks.report_competitor_matching_quality \
    --first-seen-after "${first_seen_after}" \
    --report-file "${REPORT_DIR}/competitor_matching_quality_${run_id}.json"

echo "[$(date -Iseconds)] competitor matching nightly finished successfully" >> "${LOG_FILE}"
