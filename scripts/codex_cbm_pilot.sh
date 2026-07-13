#!/usr/bin/env bash
set -euo pipefail

readonly expected_version="0.9.0"
readonly cbm_bin="${CBM_BIN:-${HOME}/.local/bin/codebase-memory-mcp}"
readonly repo_root="$(git rev-parse --show-toplevel)"
readonly cache_dir="${CBM_CACHE_DIR:-/data/cbm-indexes/pricing-service-llm-context-pilot}"

if [[ ! -x "${cbm_bin}" ]]; then
  echo "codebase-memory-mcp not found or not executable: ${cbm_bin}" >&2
  exit 1
fi

actual_version="$(${cbm_bin} --version | awk '{print $2}')"
if [[ "${actual_version}" != "${expected_version}" ]]; then
  echo "expected codebase-memory-mcp ${expected_version}, got ${actual_version}" >&2
  exit 1
fi

mkdir -p "${cache_dir}"

exec codex \
  -c "mcp_servers.codebase_memory_pilot.command=\"${cbm_bin}\"" \
  -c "mcp_servers.codebase_memory_pilot.env={CBM_ALLOWED_ROOT=\"${repo_root}\",CBM_CACHE_DIR=\"${cache_dir}\"}" \
  -c 'mcp_servers.codebase_memory_pilot.enabled_tools=["index_repository","index_status","search_graph","trace_path","detect_changes","get_code_snippet","get_architecture"]' \
  "$@"
