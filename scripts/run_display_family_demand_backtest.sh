#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/display-family-demand-backtest-2026-08-15/all-w14-b050-conservative}"

cd "$repo_root"
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export PYTHONMALLOC="${PYTHONMALLOC:-malloc}"

./.venv/bin/python -m tasks.report_display_family_demand_backtest \
  --scope all \
  --phase baseline \
  --resume \
  --output-dir "$output_dir"

./.venv/bin/python -m tasks.report_display_family_demand_backtest \
  --scope all \
  --phase candidate \
  --resume \
  --output-dir "$output_dir"
