#!/usr/bin/env bash
set -euo pipefail

LOG_DIR="/var/log/pricing"
LOG_FILE="${LOG_DIR}/expertise_sync_watchdog.log"

mkdir -p "${LOG_DIR}"

timestamp="$(date -Iseconds)"

if systemctl is-active --quiet pricing-expertise-sync.timer; then
  echo "[${timestamp}] pricing-expertise-sync.timer is active" >> "${LOG_FILE}"
  exit 0
fi

echo "[${timestamp}] pricing-expertise-sync.timer is inactive, enabling" >> "${LOG_FILE}"
systemctl enable --now pricing-expertise-sync.timer >> "${LOG_FILE}" 2>&1
systemctl is-active pricing-expertise-sync.timer >> "${LOG_FILE}" 2>&1
