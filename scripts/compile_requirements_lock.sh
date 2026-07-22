#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIP_COMPILE="${PIP_COMPILE:-$REPO_ROOT/.venv/bin/pip-compile}"

if [[ ! -x "$PIP_COMPILE" ]]; then
  echo "pip-compile is missing; install the build extra in the project virtualenv" >&2
  exit 2
fi

cd "$REPO_ROOT"
"$PIP_COMPILE" \
  --generate-hashes \
  --strip-extras \
  --resolver=backtracking \
  --output-file=requirements.lock \
  pyproject.toml
