#!/usr/bin/env bash
set -Eeuo pipefail

script_path="$(readlink -f "${BASH_SOURCE[0]}")"
repo_root="$(cd "$(dirname "$script_path")/.." && pwd)"
python_bin="$repo_root/.venv/bin/python"

if [[ "${1:-}" != "--foreground" ]]; then
    command -v systemd-run >/dev/null 2>&1 || {
        echo "systemd-run is required for a detached targeted safety backtest" >&2
        exit 1
    }
    run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    unit_name="pricing-working-targeted-$run_id"
    exec systemd-run \
        --unit="$unit_name" \
        --collect \
        --property=Type=exec \
        --property=MemoryHigh="${WORKING_TARGETED_MEMORY_HIGH:-4608M}" \
        --property=MemoryMax="${WORKING_TARGETED_MEMORY_MAX:-5632M}" \
        --property=MemorySwapMax="${WORKING_TARGETED_SWAP_MAX:-768M}" \
        --property=TasksMax=64 \
        --working-directory="$repo_root" \
        --setenv=PYTHONUNBUFFERED=1 \
        --setenv="WORKING_TARGETED_RUN_ID=$run_id" \
        "$script_path" --foreground "$@"
fi

shift
run_id="${WORKING_TARGETED_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
log_dir="$repo_root/.local/logs/working-targeted-safety"
log_path="$log_dir/$run_id.log"
output_dir="reports/assortment_lifecycle/backtest-2026-01-01_2026-07-31/working-targeted-safety-backtest-2026-08-15"
mkdir -p "$log_dir"
umask 077
exec >>"$log_path" 2>&1

echo "run_id=$run_id"
echo "started_at=$(date --iso-8601=seconds)"
echo "repo_root=$repo_root"
echo "log_path=$log_path"

cd "$repo_root"
set +e
"$python_bin" -m tasks.report_display_auto_order_working_safety_backtest \
    --experiment targeted \
    --memory-budget-mb 5120 \
    --min-free-disk-gb 5 \
    --min-swap-free-mb 256 \
    --output-dir "$output_dir" \
    "$@"
exit_code=$?
set -e
echo "finished_at=$(date --iso-8601=seconds)"
echo "exit_code=$exit_code"
exit "$exit_code"
