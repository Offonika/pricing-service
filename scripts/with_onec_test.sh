#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/pricing-service}"
ENV_FILE="${REPO_DIR}/.env"

if [[ $# -eq 0 ]]; then
  cat <<'EOF'
Usage:
  scripts/with_onec_test.sh <command> [args...]

Examples:
  scripts/with_onec_test.sh ./.venv/bin/python -m tasks.compare_employee_receivable_report --help
  scripts/with_onec_test.sh ./.venv/bin/python -m tasks.sync_receivable_ledger --help
EOF
  exit 1
fi

cd "${REPO_DIR}"

if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

if [[ -z "${ONEC_DATABASE_URL:-}" ]]; then
  echo "ONEC_DATABASE_URL is not configured" >&2
  exit 1
fi

if [[ -n "${ONEC_TEST_DATABASE_URL:-}" ]]; then
  export ONEC_DATABASE_URL="${ONEC_TEST_DATABASE_URL}"
else
  export ONEC_DATABASE_URL="${ONEC_DATABASE_URL%/*}/Ekama_test"
fi

echo "Using test 1C database: ${ONEC_DATABASE_URL##*@}" >&2
exec "$@"
