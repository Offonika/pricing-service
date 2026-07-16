#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP_COMPILE_BIN="${PIP_COMPILE_BIN:-$REPO_ROOT/.venv/bin/pip-compile}"
PIP_COMPILE_PYTHON_BIN="${PIP_COMPILE_PYTHON_BIN:-$(dirname "$PIP_COMPILE_BIN")/python}"
CONSTRAINTS_FILE="${PRICING_SERVICE_CONSTRAINTS_FILE:-/tmp/pricing-service-current-constraints.txt}"

if [[ ! -x "$PIP_COMPILE_BIN" ]]; then
  echo "pip-compile is missing; install the build extra in the project virtualenv" >&2
  exit 2
fi
if [[ ! -x "$PIP_COMPILE_PYTHON_BIN" ]]; then
  echo "Python for pip-compile is missing: $PIP_COMPILE_PYTHON_BIN" >&2
  exit 2
fi

"$PIP_COMPILE_PYTHON_BIN" -m pip freeze --all \
  | sed -E '/^-e /d; /^pricing-service==/d' \
  > "$CONSTRAINTS_FILE"
trap 'rm -f "$CONSTRAINTS_FILE"' EXIT

cd "$REPO_ROOT"
env -u PIP_COMPILE "$PIP_COMPILE_BIN" \
  --constraint="$CONSTRAINTS_FILE" \
  --generate-hashes \
  --strip-extras \
  --resolver=backtracking \
  --output-file=requirements.lock \
  pyproject.toml
