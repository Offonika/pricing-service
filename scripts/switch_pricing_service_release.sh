#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

RELEASE_DIR="${1:-}"
ACTIVE_LINK="${PRICING_SERVICE_ACTIVE_LINK:-/opt/MM/pricing-service-task43-current}"
SERVICE_NAME="${PRICING_SERVICE_SERVICE_NAME:-pricing-service.service}"
SYSTEMCTL_BIN="${PRICING_SERVICE_SYSTEMCTL_BIN:-$(command -v systemctl)}"
NGINX_BIN="${PRICING_SERVICE_NGINX_BIN:-$(command -v nginx || true)}"
SYSTEMD_PREFLIGHT="${PRICING_SERVICE_SYSTEMD_PREFLIGHT:-1}"
NGINX_PREFLIGHT="${PRICING_SERVICE_NGINX_PREFLIGHT:-1}"
FORCE_SMOKE_FAILURE="${PRICING_SERVICE_FORCE_SMOKE_FAILURE:-0}"
PYTHON_BOOTSTRAP="${PRICING_SERVICE_PYTHON_BOOTSTRAP:-python3}"
EXPECTED_ACTIVE_RELEASE="${PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE:-}"
MUTABLE_ROOT="${PRICING_SERVICE_MUTABLE_ROOT:-}"
SWITCH_LOCK_FILE="${PRICING_SERVICE_SWITCH_LOCK_FILE:-/var/lock/pricing-service-release-switch.lock}"
AUDIT_LOG_FILE="${PRICING_SERVICE_RELEASE_AUDIT_LOG:-/var/log/pricing/pricing-release-switch.jsonl}"

previous_target=""
candidate_source_commit=""
required_base_commit=""
verification_marker_tmp=""
AUDIT_READY=0

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

prepare_audit_log() {
  mkdir -p "$(dirname "$AUDIT_LOG_FILE")"
  (umask 027; : >>"$AUDIT_LOG_FILE")
  AUDIT_READY=1
}

audit_event() {
  local event="$1"
  local detail="${2:-}"
  [[ "$AUDIT_READY" == 1 ]] || return 1
  "$PYTHON_BOOTSTRAP" - \
    "$AUDIT_LOG_FILE" \
    "$event" \
    "$RELEASE_DIR" \
    "$previous_target" \
    "$candidate_source_commit" \
    "$required_base_commit" \
    "$detail" <<'PY'
import datetime
import json
import os
import sys

path, event, candidate, previous, source_commit, base_commit, detail = sys.argv[1:]
payload = {
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "event": event,
    "candidate_release": candidate or None,
    "previous_release": previous or None,
    "candidate_source_commit": source_commit or None,
    "required_base_commit": base_commit or None,
    "detail": detail or None,
}
line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o640)
try:
    os.write(descriptor, line)
finally:
    os.close(descriptor)
PY
}

reject() {
  local message="$1"
  local exit_code="${2:-2}"
  echo "$message" >&2
  if [[ "$AUDIT_READY" == 1 ]] && ! audit_event "rejected" "$message"; then
    echo "failed to append release rejection to audit log: $AUDIT_LOG_FILE" >&2
  fi
  exit "$exit_code"
}

if ! prepare_audit_log; then
  echo "release audit log is not writable: $AUDIT_LOG_FILE" >&2
  exit 2
fi
if [[ -z "$RELEASE_DIR" ]] || [[ "$RELEASE_DIR" != /* ]] || [[ ! -d "$RELEASE_DIR" ]]; then
  reject "release directory must be an existing absolute path: $RELEASE_DIR"
fi
RELEASE_DIR="$(realpath "$RELEASE_DIR")"
if [[ -z "$EXPECTED_ACTIVE_RELEASE" ]]; then
  reject "PRICING_SERVICE_EXPECTED_ACTIVE_RELEASE is required"
fi
if [[ -z "$MUTABLE_ROOT" ]]; then
  reject "PRICING_SERVICE_MUTABLE_ROOT is required"
fi
if [[ "$MUTABLE_ROOT" != /* ]] || [[ ! -d "$MUTABLE_ROOT" ]]; then
  reject "mutable root must be an existing absolute path: $MUTABLE_ROOT"
fi
MUTABLE_ROOT="$(realpath "$MUTABLE_ROOT")"

mkdir -p "$(dirname "$SWITCH_LOCK_FILE")"
exec 9>"$SWITCH_LOCK_FILE"
if ! flock -n 9; then
  reject "another pricing-service release switch is already running" 3
fi

PYTHON_BIN="$RELEASE_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  reject "release-specific Python is missing: $PYTHON_BIN"
fi
if [[ ! -f "$RELEASE_DIR/release-manifest.json" ]] || [[ ! -f "$RELEASE_DIR/requirements.lock" ]]; then
  reject "release manifest or requirements lock is missing"
fi
if [[ ! -f "$RELEASE_DIR/ui/dist/index.html" ]]; then
  reject "release UI is missing: $RELEASE_DIR/ui/dist/index.html"
fi

previous_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ -z "$previous_target" ]] || [[ ! -d "$previous_target" ]]; then
  reject "active release link has no valid rollback target: $ACTIVE_LINK"
fi
expected_target="$(readlink -f "$EXPECTED_ACTIVE_RELEASE" 2>/dev/null || true)"
if [[ -z "$expected_target" ]] || [[ "$previous_target" != "$expected_target" ]]; then
  reject "active release changed since preflight: expected $EXPECTED_ACTIVE_RELEASE, found $previous_target" 3
fi
if [[ ! -f "$previous_target/release-manifest.json" ]]; then
  reject "active release manifest is missing: $previous_target/release-manifest.json"
fi
if ! previous_source_commit="$("$PYTHON_BOOTSTRAP" - "$previous_target/release-manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(manifest.get("source_commit") or "")
PY
)"; then
  reject "active release manifest is invalid"
fi
if [[ ! "$previous_source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  reject "active release manifest has an invalid source_commit"
fi

if ! manifest_integrity="$("$PYTHON_BOOTSTRAP" - "$RELEASE_DIR/release-manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
values = (
    manifest.get("source_verified") is True,
    manifest.get("source_commit") or "",
    manifest.get("required_base_ref") or "",
    manifest.get("required_base_commit") or "",
    manifest.get("mutable_root") or "",
    manifest.get("source_dirty"),
    manifest.get("content_hash_scheme") or "",
    manifest.get("content_sha256") or "",
)
print("\t".join("true" if value is True else "false" if value is False else str(value) for value in values))
PY
)"; then
  reject "release manifest is invalid"
fi
IFS=$'\t' read -r source_verified candidate_source_commit required_base_ref \
  required_base_commit manifest_mutable_root source_dirty content_hash_scheme \
  expected_content_sha256 <<<"$manifest_integrity"

if [[ "$source_verified" != true ]]; then
  reject "release manifest does not confirm source verification"
fi
if [[ ! "$candidate_source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  reject "release manifest has an invalid source_commit"
fi
if [[ -z "$required_base_ref" ]] || [[ ! "$required_base_commit" =~ ^[0-9a-f]{40}$ ]]; then
  reject "release manifest has invalid required production base provenance"
fi
if [[ "$required_base_commit" != "$previous_source_commit" ]]; then
  reject "candidate production base does not match active source_commit: expected $previous_source_commit, found $required_base_commit" 3
fi
if [[ "$source_dirty" != false ]]; then
  reject "release manifest must declare source_dirty=false"
fi
if [[ "$manifest_mutable_root" != /* ]]; then
  reject "release manifest has an invalid mutable_root"
fi
manifest_mutable_root="$(realpath -m "$manifest_mutable_root")"
if [[ "$manifest_mutable_root" != "$MUTABLE_ROOT" ]]; then
  reject "candidate mutable_root does not match PRICING_SERVICE_MUTABLE_ROOT"
fi
for mutable_name in .local .artifacts build data reports; do
  mutable_link="$RELEASE_DIR/$mutable_name"
  expected_mutable_target="$(realpath -m "$MUTABLE_ROOT/$mutable_name")"
  actual_mutable_target="$(readlink -f "$mutable_link" 2>/dev/null || true)"
  if [[ ! -L "$mutable_link" ]] || [[ "$actual_mutable_target" != "$expected_mutable_target" ]]; then
    reject "candidate mutable path is not linked to persistent state: $mutable_name"
  fi
done
if [[ "$content_hash_scheme" != "sha256-files-v2-no-python-cache" ]]; then
  reject "unsupported release content hash scheme: ${content_hash_scheme:-missing}"
fi
if [[ ! "$expected_content_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  reject "release manifest has an invalid content_sha256"
fi
actual_content_sha256="$(hash_release_content "$RELEASE_DIR")"
if [[ "$actual_content_sha256" != "$expected_content_sha256" ]]; then
  reject "release content hash mismatch"
fi

if [[ "$SYSTEMD_PREFLIGHT" == 1 ]]; then
  if ! systemd_unit="$("$SYSTEMCTL_BIN" cat "$SERVICE_NAME")"; then
    reject "systemd service preflight failed"
  fi
  if [[ "$systemd_unit" != *"$ACTIVE_LINK/.venv/bin/python"* ]]; then
    reject "systemd service does not use the release-specific Python"
  fi
fi
if [[ "$NGINX_PREFLIGHT" == 1 ]]; then
  if [[ -z "$NGINX_BIN" ]]; then
    reject "nginx does not serve UI from the active release link"
  fi
  if ! nginx_config="$("$NGINX_BIN" -T 2>&1)"; then
    reject "nginx configuration test failed"
  fi
  if [[ "$nginx_config" != *"$ACTIVE_LINK/ui/dist"* ]]; then
    reject "nginx does not serve UI from the active release link"
  fi
fi

if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/export_openapi.py --check
); then
  reject "candidate OpenAPI contract is stale"
fi
if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_release_api_compatibility.py \
    --baseline-dir "$previous_target"
); then
  reject "candidate removes production API operations"
fi
if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_executive_dashboard_release.py \
    --skip-database-revision
); then
  reject "candidate executive dashboard release validation failed"
fi
if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_receivables_release.py \
    --release-dir "$RELEASE_DIR"
); then
  reject "candidate receivables release validation failed"
fi

chmod -R a+rX "$RELEASE_DIR/ui/dist"

current_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ "$current_target" != "$previous_target" ]]; then
  reject "active release changed during preflight: expected $previous_target, found $current_target" 3
fi
if ! audit_event "attempt" "all strict preflight checks passed"; then
  reject "failed to append release attempt to audit log"
fi

rollback() {
  local failure_status=$?
  local rollback_detail="post-switch verification failed; rollback was not needed"
  trap - ERR
  set +e
  if [[ -n "$verification_marker_tmp" ]]; then
    rm -f "$verification_marker_tmp"
  fi
  current_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
  if [[ "$current_target" == "$RELEASE_DIR" ]] && [[ -d "$previous_target" ]]; then
    rollback_link="${ACTIVE_LINK}.rollback.$$"
    ln -s "$previous_target" "$rollback_link"
    mv -Tf "$rollback_link" "$ACTIVE_LINK"
    if "$SYSTEMCTL_BIN" restart "$SERVICE_NAME"; then
      rollback_detail="candidate failed verification; active link and service restored"
    else
      rollback_detail="candidate failed verification; active link restored but service restart failed"
    fi
    audit_event "rolled_back" "$rollback_detail" || true
  else
    audit_event "rejected" "$rollback_detail" || true
  fi
  exit "$failure_status"
}
trap rollback ERR

if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" -m alembic upgrade head
); then
  reject "candidate database migration failed"
fi
if ! (
  cd "$RELEASE_DIR"
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$RELEASE_DIR" \
    "$PYTHON_BIN" scripts/validate_executive_dashboard_release.py
); then
  reject "candidate database revision validation failed after migration"
fi

current_target="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"
if [[ "$current_target" != "$previous_target" ]]; then
  reject "active release changed during database migration: expected $previous_target, found $current_target" 3
fi

next_link="${ACTIVE_LINK}.next.$$"
ln -s "$RELEASE_DIR" "$next_link"
mv -Tf "$next_link" "$ACTIVE_LINK"

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
verification_marker_tmp="$(dirname "$RELEASE_DIR")/.$(basename "$RELEASE_DIR").release-verified.tmp.$$"
rm -f "$verification_marker"
printf '%s\n' "verified_at=$(date -Is)" >"$verification_marker_tmp"
chmod 0444 "$verification_marker_tmp"
mv -f "$verification_marker_tmp" "$verification_marker"
verification_marker_tmp=""
audit_event "verified" "release smoke check passed and marker was refreshed"

trap - ERR
echo "active pricing-service release: $RELEASE_DIR"
