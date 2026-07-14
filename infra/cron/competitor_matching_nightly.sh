#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"
ENV_LOADER="${REPO_DIR}/infra/cron/load_env.sh"
PYTHON_BIN="${REPO_DIR}/.venv/bin/python"

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
LOG_FILE="${LOG_DIR}/competitor_matching_nightly.log"
REPORT_DIR="${REPORT_DIR:-${REPO_DIR}/build/logs}"
LOCK_FILE="${COMPETITOR_MATCHING_LOCK_FILE:-/var/run/pricing_competitor_matching_nightly.lock}"
LATEST_REPORT="${REPORT_DIR}/competitor_matching_nightly_latest.json"

LOOKBACK_DAYS="${COMPETITOR_MATCHING_LOOKBACK_DAYS:-3}"
EMBED_MODEL="${COMPETITOR_MATCHING_EMBED_MODEL:-text-embedding-3-small}"
EMBED_EXPECTED_DIM="${COMPETITOR_MATCHING_EMBED_EXPECTED_DIM:-1536}"
EMBEDDINGS_ENABLED="${COMPETITOR_MATCHING_EMBEDDINGS_ENABLED:-1}"
CHAT_LLM_ENABLED="${COMPETITOR_MATCHING_CHAT_LLM_ENABLED:-0}"
FTP_LLM_LIMIT="${COMPETITOR_MATCHING_FTP_LLM_LIMIT:-0}"
FTP_MAX_LAG_DAYS="${COMPETITOR_MATCHING_FTP_MAX_LAG_DAYS:-1}"
COMPAT_LLM_LIMIT="${COMPETITOR_MATCHING_COMPAT_LLM_LIMIT:-0}"
LLM_ARBITER_ENABLED="${COMPETITOR_MATCHING_LLM_ARBITER_ENABLED:-0}"
MATCH_FTP_LATEST_ONLY="${COMPETITOR_MATCHING_FTP_LATEST_ONLY:-1}"
MATCH_FTP_PROGRESS_EVERY="${COMPETITOR_MATCHING_FTP_PROGRESS_EVERY:-5000}"
PRODUCT_BACKFILL_ENABLED="${COMPETITOR_MATCHING_PRODUCT_BACKFILL_ENABLED:-0}"
AUTO_ACCEPT_MIN_SCORE="${COMPETITOR_MATCHING_AUTO_ACCEPT_MIN_SCORE:-0.80}"
LIVE_CACHE_LIMIT="${COMPETITOR_MATCHING_LIVE_CACHE_LIMIT:-3000}"
LIVE_CACHE_MAX_SECONDS="${COMPETITOR_MATCHING_LIVE_CACHE_MAX_SECONDS:-7200}"
LIVE_CACHE_FAST_ONLY="${COMPETITOR_MATCHING_LIVE_CACHE_FAST_ONLY:-1}"
LIVE_CACHE_FULL_REFRESH_ENABLED="${COMPETITOR_MATCHING_LIVE_CACHE_FULL_REFRESH_ENABLED:-1}"
LIVE_CACHE_FULL_LIMIT="${COMPETITOR_MATCHING_LIVE_CACHE_FULL_LIMIT:-3000}"
LIVE_CACHE_FULL_MAX_SECONDS="${COMPETITOR_MATCHING_LIVE_CACHE_FULL_MAX_SECONDS:-1200}"
URL_ALIAS_TIMEOUT="${COMPETITOR_MATCHING_URL_ALIAS_TIMEOUT:-8}"
URL_ALIAS_BACKFILL_LIMIT="${COMPETITOR_MATCHING_URL_ALIAS_BACKFILL_LIMIT:-2000}"
REVIEW_QUEUE_MIN_SCORE="${COMPETITOR_MATCHING_REVIEW_QUEUE_MIN_SCORE:-0.80}"
REVIEW_QUEUE_MIN_GAP="${COMPETITOR_MATCHING_REVIEW_QUEUE_MIN_GAP:-0.02}"
REVIEW_QUEUE_ALTERNATIVES_LIMIT="${COMPETITOR_MATCHING_REVIEW_QUEUE_ALTERNATIVES_LIMIT:-5}"
REVIEW_QUEUE_GLOBAL_LIMIT="${COMPETITOR_MATCHING_REVIEW_QUEUE_GLOBAL_LIMIT:-0}"
FORCE_RUN="${COMPETITOR_MATCHING_FORCE_RUN:-0}"

mkdir -p "${LOG_DIR}" "${REPORT_DIR}"
cd "${REPO_DIR}"

first_seen_after="${COMPETITOR_MATCHING_FIRST_SEEN_AFTER:-$(date -d "${LOOKBACK_DAYS} days ago" +%F)}"
run_id="$(date +%Y%m%d_%H%M%S)"
overall_status="running"
embedding_status="not_checked"
ftp_status="not_checked"
current_step="init"
finished=0

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] competitor matching nightly is already running; skipping" >> "${LOG_FILE}"
  exit 0
fi

write_latest_report() {
  local exit_code="${1:-0}"
  "${PYTHON_BIN}" - "${LATEST_REPORT}" <<PY
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "run_id": "${run_id}",
    "status": "${overall_status}",
    "exit_code": int("${exit_code}"),
    "current_step": "${current_step}",
    "embedding_status": "${embedding_status}",
    "embeddings_enabled": "${EMBEDDINGS_ENABLED}",
    "ftp_status": "${ftp_status}",
    "chat_llm_enabled": "${CHAT_LLM_ENABLED}",
    "product_backfill_enabled": "${PRODUCT_BACKFILL_ENABLED}",
    "lookback_days": int("${LOOKBACK_DAYS}"),
    "first_seen_after": "${first_seen_after}",
    "embed_model": "${EMBED_MODEL}",
    "finished_at": datetime.now(timezone.utc).isoformat(),
}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

already_succeeded_today() {
  "${PYTHON_BIN}" - "${LATEST_REPORT}" <<'PY'
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(1)
if payload.get("status") not in {"success", "degraded_source_stale"}:
    raise SystemExit(1)
finished_at = payload.get("finished_at")
if not finished_at:
    raise SystemExit(1)
try:
    finished = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
except ValueError:
    raise SystemExit(1)
if finished.tzinfo is None:
    finished = finished.replace(tzinfo=timezone.utc)
moscow = timezone(timedelta(hours=3))
if finished.astimezone(moscow).date() == datetime.now(moscow).date():
    raise SystemExit(0)
raise SystemExit(1)
PY
}

on_exit() {
  local exit_code=$?
  if [[ "${finished}" -ne 1 ]]; then
    overall_status="failed"
    write_latest_report "${exit_code}" || true
  fi
}
trap on_exit EXIT

if [[ "${FORCE_RUN}" != "1" ]] && already_succeeded_today; then
  overall_status="skipped_success_today"
  current_step="skipped"
  finished=1
  echo "[$(date -Iseconds)] competitor matching nightly already completed today; skipping retry" >> "${LOG_FILE}"
  exit 0
fi

timestamp="$(date -Iseconds)"
{
  echo "[$timestamp] starting competitor matching nightly"
  echo "[$timestamp] lookback_days=${LOOKBACK_DAYS} first_seen_after=${first_seen_after} embed_model=${EMBED_MODEL}"
  echo "[$timestamp] embeddings_enabled=${EMBEDDINGS_ENABLED} chat_llm_enabled=${CHAT_LLM_ENABLED}"
  echo "[$timestamp] match_ftp_latest_only=${MATCH_FTP_LATEST_ONLY} match_ftp_progress_every=${MATCH_FTP_PROGRESS_EVERY}"
  echo "[$timestamp] product_backfill_enabled=${PRODUCT_BACKFILL_ENABLED}"
  echo "[$timestamp] live_cache_fast_only=${LIVE_CACHE_FAST_ONLY} live_cache_limit=${LIVE_CACHE_LIMIT}"
  echo "[$timestamp] live_cache_full_refresh_enabled=${LIVE_CACHE_FULL_REFRESH_ENABLED} live_cache_full_limit=${LIVE_CACHE_FULL_LIMIT} live_cache_full_max_seconds=${LIVE_CACHE_FULL_MAX_SECONDS}"
  echo "[$timestamp] url_alias_backfill_limit=${URL_ALIAS_BACKFILL_LIMIT} url_alias_timeout=${URL_ALIAS_TIMEOUT}"
  echo "[$timestamp] review_queue_min_score=${REVIEW_QUEUE_MIN_SCORE} review_queue_min_gap=${REVIEW_QUEUE_MIN_GAP} review_queue_alternatives_limit=${REVIEW_QUEUE_ALTERNATIVES_LIMIT} review_queue_global_limit=${REVIEW_QUEUE_GLOBAL_LIMIT}"
  echo "[$timestamp] python interpreter: ${PYTHON_BIN}"
} >> "${LOG_FILE}"

run_step() {
  local step_name="$1"
  shift
  local tmp_output
  tmp_output="$(mktemp)"
  current_step="${step_name}"

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
if run_step "sync_onec_product_catalog" \
  "${PYTHON_BIN}" -m tasks.sync_onec_product_catalog; then
  if [[ "${PRODUCT_BACKFILL_ENABLED}" == "1" ]]; then
    run_step "product_phone_model_backfill" \
      "${PYTHON_BIN}" -m tasks.backfill_phone_model_links \
        --products \
        --batch-size 1000 \
        --progress-every 5000
  else
    echo "[$(date -Iseconds)] skipping product_phone_model_backfill; product import already syncs phone model links" >> "${LOG_FILE}"
  fi
else
  product_refresh_ok=0
  echo "[$(date -Iseconds)] product refresh failed; FTP catalog refresh will continue, matching will be skipped" >> "${LOG_FILE}"
fi

case "${COMPETITOR_HTTP_IMPORT_ENABLED:-false}" in
  1|true|TRUE|yes|YES)
    run_step "import_competitor_http" \
      "${PYTHON_BIN}" -m tasks.import_competitor_http
    ;;
  *)
    run_step "import_competitor_ftp" \
      "${PYTHON_BIN}" -m tasks.import_competitor_ftp
    ;;
esac

current_step="check_competitor_ftp_freshness"
set +e
"${PYTHON_BIN}" -m tasks.competitor_matching_watchdog \
  --ftp-only \
  --max-ftp-lag-days "${FTP_MAX_LAG_DAYS}" \
  --no-telegram \
  >> "${LOG_FILE}" 2>&1
ftp_freshness_exit=$?
set -e
case "${ftp_freshness_exit}" in
  0)
    ftp_status="fresh"
    echo "[$(date -Iseconds)] competitor data quality check finished: ftp=fresh" >> "${LOG_FILE}"
    ;;
  3)
    ftp_status="stale"
    echo "[$(date -Iseconds)] competitor data quality degraded: ftp=stale" >> "${LOG_FILE}"
    ;;
  *)
    echo "[$(date -Iseconds)] competitor FTP freshness check failed technically (status=${ftp_freshness_exit})" >> "${LOG_FILE}"
    exit "${ftp_freshness_exit}"
    ;;
esac

match_ftp_args=(
  -m tasks.match_competitor_ftp
  --days-back "${LOOKBACK_DAYS}"
  --disable-category-llm
  --skip-display-attrs
)
if [[ "${MATCH_FTP_LATEST_ONLY}" == "1" ]]; then
  match_ftp_args+=(--latest-only)
fi
if [[ "${MATCH_FTP_PROGRESS_EVERY}" != "0" ]]; then
  match_ftp_args+=(--progress-every "${MATCH_FTP_PROGRESS_EVERY}")
fi
if [[ "${CHAT_LLM_ENABLED}" == "1" && "${FTP_LLM_LIMIT}" != "0" ]]; then
  match_ftp_args+=(--llm --llm-limit "${FTP_LLM_LIMIT}")
fi
run_step "match_competitor_ftp" "${PYTHON_BIN}" "${match_ftp_args[@]}"

run_step "refresh_competitor_url_aliases" \
  "${PYTHON_BIN}" -m tasks.refresh_competitor_url_aliases \
    --source moba \
    --first-seen-after "${first_seen_after}" \
    --resolve-poiskzip \
    --only-missing-direct \
    --timeout "${URL_ALIAS_TIMEOUT}"

if [[ "${URL_ALIAS_BACKFILL_LIMIT}" != "0" ]]; then
  run_step "refresh_competitor_url_aliases_backfill" \
    "${PYTHON_BIN}" -m tasks.refresh_competitor_url_aliases \
      --source moba \
      --resolve-poiskzip \
      --only-missing-direct \
      --limit "${URL_ALIAS_BACKFILL_LIMIT}" \
      --timeout "${URL_ALIAS_TIMEOUT}" \
      --batch-size 100
else
  echo "[$(date -Iseconds)] skipping URL alias backfill; COMPETITOR_MATCHING_URL_ALIAS_BACKFILL_LIMIT=0" >> "${LOG_FILE}"
fi

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
  compat_args=(
    -m tasks.match_competitor_items
    --first-seen-after "${first_seen_after}"
    --only-missing-compat
    --batch-size 1000
  )
  if [[ "${CHAT_LLM_ENABLED}" == "1" && "${COMPAT_LLM_LIMIT}" != "0" ]]; then
    compat_args+=(--llm --llm-limit "${COMPAT_LLM_LIMIT}")
  fi
  run_step "match_competitor_items_compatibility" "${PYTHON_BIN}" "${compat_args[@]}"

  run_step "competitor_phone_model_backfill" \
    "${PYTHON_BIN}" -m tasks.backfill_phone_model_links \
      --competitors \
      --first-seen-after "${first_seen_after}" \
      --batch-size 1000 \
      --progress-every 1000

  if [[ "${EMBEDDINGS_ENABLED}" != "1" ]]; then
    embedding_status="disabled"
    echo "[$(date -Iseconds)] embeddings disabled by COMPETITOR_MATCHING_EMBEDDINGS_ENABLED=${EMBEDDINGS_ENABLED}; embedding matching skipped" >> "${LOG_FILE}"
  elif run_step "embedding_preflight" \
    "${PYTHON_BIN}" -m tasks.check_embeddings \
      --embed-model "${EMBED_MODEL}" \
      --expected-dim "${EMBED_EXPECTED_DIM}"; then
    embedding_status="ready"

    run_step "compute_embeddings" \
      "${PYTHON_BIN}" -m tasks.compute_embeddings \
        --target both \
        --only-changed \
        --embed-model "${EMBED_MODEL}"

    embedding_match_args=(
      -m tasks.match_competitor_items_embeddings
      --only-null
      --first-seen-after "${first_seen_after}"
      --auto-accept-min-score "${AUTO_ACCEPT_MIN_SCORE}"
      --no-auto-accept-unique
      --report-file "${REPORT_DIR}/match_competitor_items_embeddings_${run_id}.json"
      --report-csv "${REPORT_DIR}/match_competitor_items_embeddings_${run_id}.csv"
    )
    if [[ "${CHAT_LLM_ENABLED}" == "1" && "${LLM_ARBITER_ENABLED}" == "1" ]]; then
      embedding_match_args+=(--use-llm-arbiter)
    fi
    run_step "match_competitor_items_embeddings" "${PYTHON_BIN}" "${embedding_match_args[@]}"
  else
    embedding_status="blocked"
    overall_status="blocked_embeddings"
    echo "[$(date -Iseconds)] embeddings unavailable; imported catalog is kept, embedding matching skipped" >> "${LOG_FILE}"
  fi

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
        --min-score "${REVIEW_QUEUE_MIN_SCORE}" \
        --min-gap "${REVIEW_QUEUE_MIN_GAP}" \
        --alternatives-limit "${REVIEW_QUEUE_ALTERNATIVES_LIMIT}" \
        --report-file "${REPORT_DIR}/competitor_match_review_queue_${run_id}.json" \
        --report-csv "${REPORT_DIR}/competitor_match_review_queue_${run_id}.csv"

  if [[ "${REVIEW_QUEUE_GLOBAL_LIMIT}" != "0" ]]; then
    run_step "export_competitor_match_review_queue_global" \
        "${PYTHON_BIN}" -m tasks.export_competitor_match_review_queue \
          --limit "${REVIEW_QUEUE_GLOBAL_LIMIT}" \
          --min-score "${REVIEW_QUEUE_MIN_SCORE}" \
          --min-gap "${REVIEW_QUEUE_MIN_GAP}" \
          --alternatives-limit "${REVIEW_QUEUE_ALTERNATIVES_LIMIT}" \
          --report-file "${REPORT_DIR}/competitor_match_review_queue_global_${run_id}.json" \
          --report-csv "${REPORT_DIR}/competitor_match_review_queue_global_${run_id}.csv"
  else
    echo "[$(date -Iseconds)] skipping global review queue export; COMPETITOR_MATCHING_REVIEW_QUEUE_GLOBAL_LIMIT=0" >> "${LOG_FILE}"
  fi

  live_cache_args=(
    -m tasks.refresh_live_candidate_cache
    --limit "${LIVE_CACHE_LIMIT}"
    --max-seconds "${LIVE_CACHE_MAX_SECONDS}"
    --batch-size 500
    --progress-every 1000
    --report-file "${REPORT_DIR}/live_candidate_cache_${run_id}.json"
  )
  if [[ "${LIVE_CACHE_FAST_ONLY}" == "1" ]]; then
    live_cache_args+=(--fast-only)
  fi
  run_step "refresh_live_candidate_cache" "${PYTHON_BIN}" "${live_cache_args[@]}"

  if [[ "${LIVE_CACHE_FULL_REFRESH_ENABLED}" == "1" ]]; then
    run_step "refresh_live_candidate_cache_full_bounded" \
      "${PYTHON_BIN}" -m tasks.refresh_live_candidate_cache \
        --limit "${LIVE_CACHE_FULL_LIMIT}" \
        --max-seconds "${LIVE_CACHE_FULL_MAX_SECONDS}" \
        --batch-size 100 \
        --progress-every 500 \
        --report-file "${REPORT_DIR}/live_candidate_cache_full_${run_id}.json"
  else
    echo "[$(date -Iseconds)] skipping full live candidate cache refresh; COMPETITOR_MATCHING_LIVE_CACHE_FULL_REFRESH_ENABLED=${LIVE_CACHE_FULL_REFRESH_ENABLED}" >> "${LOG_FILE}"
  fi
else
  overall_status="product_refresh_failed"
  echo "[$(date -Iseconds)] skipping compatibility/embeddings/matching because product refresh failed" >> "${LOG_FILE}"
fi

run_step "report_competitor_matching_quality" \
  "${PYTHON_BIN}" -m tasks.report_competitor_matching_quality \
    --first-seen-after "${first_seen_after}" \
    --report-file "${REPORT_DIR}/competitor_matching_quality_${run_id}.json"

if [[ "${overall_status}" == "running" ]]; then
  if [[ "${ftp_status}" == "fresh" ]]; then
    overall_status="success"
  else
    overall_status="degraded_source_stale"
  fi
fi
current_step="finished"
write_latest_report "0"
finished=1
echo "[$(date -Iseconds)] competitor matching nightly finished with status=${overall_status}" >> "${LOG_FILE}"
