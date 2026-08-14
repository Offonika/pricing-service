#!/usr/bin/env bash
set -Eeuo pipefail

script_path="$(readlink -f "${BASH_SOURCE[0]}")"
repo_root="$(cd "$(dirname "$script_path")/.." && pwd)"
python_bin="$repo_root/.venv/bin/python"

if [[ "${1:-}" != "--foreground" ]]; then
    command -v systemd-run >/dev/null 2>&1 || {
        echo "systemd-run is required for a detached economic backtest" >&2
        exit 1
    }
    run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
    unit_name="pricing-assortment-economic-$run_id"
    exec systemd-run \
        --unit="$unit_name" \
        --collect \
        --property=Type=exec \
        --property=MemoryHigh="${ASSORTMENT_ECONOMIC_MEMORY_HIGH:-3200M}" \
        --property=MemoryMax="${ASSORTMENT_ECONOMIC_MEMORY_MAX:-4096M}" \
        --property=MemorySwapMax="${ASSORTMENT_ECONOMIC_SWAP_MAX:-512M}" \
        --property=TasksMax=64 \
        --working-directory="$repo_root" \
        --setenv=PYTHONUNBUFFERED=1 \
        --setenv="ASSORTMENT_ECONOMIC_RUN_ID=$run_id" \
        "$script_path" --foreground "$@"
fi

shift
run_id="${ASSORTMENT_ECONOMIC_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
log_dir="$repo_root/.local/logs/assortment-lifecycle-economic"
log_path="$log_dir/$run_id.log"
mkdir -p "$log_dir"
umask 077
exec >>"$log_path" 2>&1

echo "run_id=$run_id"
echo "started_at=$(date --iso-8601=seconds)"
echo "repo_root=$repo_root"
echo "log_path=$log_path"

cd "$repo_root"
set +e
"$python_bin" -m tasks.run_assortment_lifecycle_v2_economic_backtest "$@"
exit_code=$?
set -e
echo "finished_at=$(date --iso-8601=seconds)"
echo "exit_code=$exit_code"
exit "$exit_code"
