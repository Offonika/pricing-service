#!/usr/bin/env bash
set -euo pipefail

export TZ="${TZ:-Europe/Moscow}"

SUCCESS_FILE="${LOGISTICS_RTU_SYNC_SUCCESS_FILE:-/var/lib/pricing-service/logistics_rtu_sync.last_success}"
MAX_AGE_SECONDS="${LOGISTICS_RTU_SYNC_MAX_AGE_SECONDS:-180}"

if [[ ! -f "${SUCCESS_FILE}" ]]; then
  echo "[$(date -Iseconds)] CRITICAL logistics_rtu_sync has no successful-run marker"
  exit 1
fi

now_epoch="$(date +%s)"
success_epoch="$(stat -c %Y "${SUCCESS_FILE}")"
age_seconds="$(( now_epoch - success_epoch ))"
if [[ "${age_seconds}" -gt "${MAX_AGE_SECONDS}" ]]; then
  echo "[$(date -Iseconds)] CRITICAL logistics_rtu_sync is stale age_seconds=${age_seconds} max_age_seconds=${MAX_AGE_SECONDS}"
  exit 1
fi
