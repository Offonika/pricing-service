#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR="${1:?usage: switch_pricing_service_release.sh /absolute/release/dir}"
ACTIVE_LINK="${PRICING_SERVICE_ACTIVE_LINK:-/opt/MM/pricing-service-task43-current}"
PYTHON_BIN="${PRICING_SERVICE_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
SERVICE_NAME="${PRICING_SERVICE_SERVICE_NAME:-pricing-service.service}"
STATIC_ROOT="${PRICING_SERVICE_STATIC_ROOT:-/var/www/pricing-service}"
RSYNC_BIN="${PRICING_SERVICE_RSYNC_BIN:-$(command -v rsync)}"

if [[ "$RELEASE_DIR" != /* ]] || [[ ! -d "$RELEASE_DIR" ]]; then
  echo "release directory must be an existing absolute path: $RELEASE_DIR" >&2
  exit 2
fi

previous_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"

(
  cd "$RELEASE_DIR"
  PYTHONPATH="$RELEASE_DIR" "$PYTHON_BIN" scripts/validate_executive_dashboard_release.py
  PYTHONPATH="$RELEASE_DIR" "$PYTHON_BIN" scripts/validate_receivables_release.py \
    --release-dir "$RELEASE_DIR"
)

if [[ -d "$RELEASE_DIR/ui/dist" ]]; then
  chmod -R a+rX "$RELEASE_DIR/ui/dist"
  mkdir -p "$STATIC_ROOT"
  "$RSYNC_BIN" -a --delete "$RELEASE_DIR/ui/dist/" "$STATIC_ROOT/"
  chmod -R a+rX "$STATIC_ROOT"
fi

next_link="${ACTIVE_LINK}.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$ACTIVE_LINK"

rollback() {
  if [[ -n "$previous_target" ]] && [[ -d "$previous_target" ]]; then
    rollback_link="${ACTIVE_LINK}.rollback.$$"
    ln -s "$previous_target" "$rollback_link"
    mv -Tf "$rollback_link" "$ACTIVE_LINK"
    systemctl restart "$SERVICE_NAME"
  fi
}
trap rollback ERR

systemctl restart "$SERVICE_NAME"

ready=0
for _ in $(seq 1 30); do
  if (
    cd "$RELEASE_DIR"
    PYTHONPATH="$RELEASE_DIR" "$PYTHON_BIN" scripts/check_executive_dashboard_runtime.py --mode release
  ); then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" != 1 ]]; then
  echo "release smoke check failed" >&2
  false
fi

trap - ERR
echo "active pricing-service release: $RELEASE_DIR"
