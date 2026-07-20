#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR="${1:?usage: switch_pricing_service_release.sh /absolute/release/dir}"
ACTIVE_LINK="${PRICING_SERVICE_ACTIVE_LINK:-/opt/MM/pricing-service-task43-current}"
SERVICE_NAME="${PRICING_SERVICE_SERVICE_NAME:-pricing-service.service}"
SYSTEMCTL_BIN="${PRICING_SERVICE_SYSTEMCTL_BIN:-$(command -v systemctl)}"
NGINX_BIN="${PRICING_SERVICE_NGINX_BIN:-$(command -v nginx || true)}"
SYSTEMD_PREFLIGHT="${PRICING_SERVICE_SYSTEMD_PREFLIGHT:-1}"
NGINX_PREFLIGHT="${PRICING_SERVICE_NGINX_PREFLIGHT:-1}"
FORCE_SMOKE_FAILURE="${PRICING_SERVICE_FORCE_SMOKE_FAILURE:-0}"

if [[ "$RELEASE_DIR" != /* ]] || [[ ! -d "$RELEASE_DIR" ]]; then
  echo "release directory must be an existing absolute path: $RELEASE_DIR" >&2
  exit 2
fi
PYTHON_BIN="$RELEASE_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "release-specific Python is missing: $PYTHON_BIN" >&2
  exit 2
fi
if [[ ! -f "$RELEASE_DIR/release-manifest.json" ]] || [[ ! -f "$RELEASE_DIR/requirements.lock" ]]; then
  echo "release manifest or requirements lock is missing" >&2
  exit 2
fi
if [[ ! -f "$RELEASE_DIR/ui/dist/index.html" ]]; then
  echo "release UI is missing: $RELEASE_DIR/ui/dist/index.html" >&2
  exit 2
fi

if [[ "$SYSTEMD_PREFLIGHT" == 1 ]]; then
  systemd_unit="$("$SYSTEMCTL_BIN" cat "$SERVICE_NAME")"
  if [[ "$systemd_unit" != *"$ACTIVE_LINK/.venv/bin/python"* ]]; then
    echo "systemd service does not use the release-specific Python" >&2
    exit 2
  fi
fi
if [[ "$NGINX_PREFLIGHT" == 1 ]]; then
  if [[ -z "$NGINX_BIN" ]]; then
    echo "nginx does not serve UI from the active release link" >&2
    exit 2
  fi
  nginx_config="$("$NGINX_BIN" -T 2>&1 || true)"
  if [[ "$nginx_config" != *"$ACTIVE_LINK/ui/dist"* ]]; then
    echo "nginx does not serve UI from the active release link" >&2
    exit 2
  fi
fi

previous_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ -z "$previous_target" ]] || [[ ! -d "$previous_target" ]]; then
  echo "active release link has no valid rollback target: $ACTIVE_LINK" >&2
  exit 2
fi

(
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_executive_dashboard_release.py
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_receivables_release.py \
    --release-dir "$RELEASE_DIR"
)

chmod -R a+rX "$RELEASE_DIR/ui/dist"

next_link="${ACTIVE_LINK}.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$ACTIVE_LINK"

rollback() {
  trap - ERR
  if [[ -n "$previous_target" ]] && [[ -d "$previous_target" ]]; then
    rollback_link="${ACTIVE_LINK}.rollback.$$"
    ln -s "$previous_target" "$rollback_link"
    mv -Tf "$rollback_link" "$ACTIVE_LINK"
    "$SYSTEMCTL_BIN" restart "$SERVICE_NAME"
  fi
}
trap rollback ERR

"$SYSTEMCTL_BIN" restart "$SERVICE_NAME"

if [[ "$FORCE_SMOKE_FAILURE" == 1 ]]; then
  echo "forced release smoke failure" >&2
  false
fi

ready=0
for _ in $(seq 1 30); do
  if (
    cd "$RELEASE_DIR"
    PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
      "$PYTHON_BIN" scripts/check_executive_dashboard_runtime.py --mode release
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
