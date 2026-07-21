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
PYTHON_BOOTSTRAP="${PRICING_SERVICE_PYTHON_BOOTSTRAP:-python3}"
EXPECTED_ACTIVE_RELEASE="${PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE:-}"
SWITCH_LOCK_FILE="${PRICING_SERVICE_SWITCH_LOCK_FILE:-/var/lock/pricing-service-release-switch.lock}"

hash_release_content() {
  local release_dir="$1"
  {
    cd "$release_dir"
    find . -type f \
      ! -path './release-manifest.json' \
      ! -path './.release-verified' \
      ! -path '*/__pycache__/*' \
      ! -name '*.pyc' \
      ! -name '*.pyo' \
      -print0 | sort -z | xargs -0 sha256sum
  } | sha256sum | awk '{print $1}'
}

if [[ "$RELEASE_DIR" != /* ]] || [[ ! -d "$RELEASE_DIR" ]]; then
  echo "release directory must be an existing absolute path: $RELEASE_DIR" >&2
  exit 2
fi

mkdir -p "$(dirname "$SWITCH_LOCK_FILE")"
exec 9>"$SWITCH_LOCK_FILE"
if ! flock -n 9; then
  echo "another pricing-service release switch is already running" >&2
  exit 3
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

manifest_integrity="$("$PYTHON_BOOTSTRAP" - "$RELEASE_DIR/release-manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    f"{manifest.get('content_hash_scheme') or ''}\t"
    f"{manifest.get('content_sha256') or ''}"
)
PY
)"
IFS=$'\t' read -r content_hash_scheme expected_content_sha256 <<<"$manifest_integrity"
if [[ "$content_hash_scheme" == "sha256-files-v2-no-python-cache" ]]; then
  if [[ ! "$expected_content_sha256" =~ ^[0-9a-f]{64}$ ]]; then
    echo "release manifest has an invalid content_sha256" >&2
    exit 2
  fi
  actual_content_sha256="$(hash_release_content "$RELEASE_DIR")"
  if [[ "$actual_content_sha256" != "$expected_content_sha256" ]]; then
    echo "release content hash mismatch" >&2
    exit 2
  fi
elif [[ -z "$content_hash_scheme" ]]; then
  echo "warning: legacy release content hash is not runtime-stable; verification skipped" >&2
else
  echo "unsupported release content hash scheme: $content_hash_scheme" >&2
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
  if ! nginx_config="$("$NGINX_BIN" -T 2>&1)"; then
    echo "nginx configuration test failed" >&2
    exit 2
  fi
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
if [[ -n "$EXPECTED_ACTIVE_RELEASE" ]]; then
  expected_target="$(readlink -f "$EXPECTED_ACTIVE_RELEASE" 2>/dev/null || true)"
  if [[ -z "$expected_target" ]] || [[ "$previous_target" != "$expected_target" ]]; then
    echo "active release changed since preflight: expected $EXPECTED_ACTIVE_RELEASE, found $previous_target" >&2
    exit 3
  fi
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

current_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ "$current_target" != "$previous_target" ]]; then
  echo "active release changed during preflight: expected $previous_target, found $current_target" >&2
  exit 3
fi

next_link="${ACTIVE_LINK}.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$ACTIVE_LINK"

rollback() {
  trap - ERR
  current_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
  if [[ "$current_target" == "$RELEASE_DIR" ]] && [[ -n "$previous_target" ]] && [[ -d "$previous_target" ]]; then
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

verification_marker="$RELEASE_DIR/.release-verified"
printf '%s\n' "verified_at=$(date -Is)" >"$verification_marker"
chmod 0444 "$verification_marker"

trap - ERR
echo "active pricing-service release: $RELEASE_DIR"
