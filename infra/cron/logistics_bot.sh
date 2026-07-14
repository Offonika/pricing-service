#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service}"

cd "${REPO_DIR}"

exec "${REPO_DIR}/.venv/bin/python" -m app.telegram.logistics_bot
