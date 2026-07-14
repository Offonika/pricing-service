#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="/opt/MM/pricing-service"

cd "${ROOT_DIR}"
exec "${ROOT_DIR}/.venv/bin/python" -m tasks.sync_telephony_mapping "$@"
