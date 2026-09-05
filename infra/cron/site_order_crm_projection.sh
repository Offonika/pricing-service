#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
MODE="${1:-incremental}"
LOCK_FILE="${ORDER_FULFILLMENT_CRM_PROJECTION_LOCK_FILE:-/var/run/site_order_crm_projection.lock}"

cd "${REPO_DIR}"
if [[ -f "${REPO_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${REPO_DIR}/.env"
  set +a
fi

args=()
if [[ "${MODE}" == "full" ]]; then
  args+=(--full)
elif [[ "${MODE}" != "incremental" ]]; then
  echo "usage: $0 [incremental|full]" >&2
  exit 2
fi

exec flock -n "${LOCK_FILE}" ./.venv/bin/python -m tasks.refresh_site_order_crm_projection "${args[@]}"
