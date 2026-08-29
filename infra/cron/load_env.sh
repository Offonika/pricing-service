#!/usr/bin/env bash

load_env_file_preserve_json() {
  local env_file="$1"
  [[ -f "${env_file}" ]] || return 0

  local py_bin="${PYTHON_BIN:-}"
  if [[ -z "${py_bin}" || ! -x "${py_bin}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
      py_bin="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
      py_bin="$(command -v python)"
    else
      echo "python interpreter not found for env loader" >&2
      return 1
    fi
  fi

  local exports
  if ! exports="$("${py_bin}" - "${env_file}" <<'PY'
import shlex
import re
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
for raw in env_path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
        raise SystemExit("invalid environment variable name in env file")
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    print(f"export {key}={shlex.quote(value)}")
PY
)"; then
    return 1
  fi

  eval "${exports}"
}
