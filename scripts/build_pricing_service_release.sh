#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${PRICING_SERVICE_SOURCE_ROOT:-/opt/MM/pricing-service}"
RELEASE_ROOT="${PRICING_SERVICE_RELEASE_ROOT:-/opt/MM/releases/pricing-service}"
PYTHON_BIN="${PRICING_SERVICE_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
RELEASE_NAME="${1:-architecture-hardening-$(date +%Y%m%d-%H%M%S)}"
FINAL_DIR="${RELEASE_ROOT}/${RELEASE_NAME}"
TEMP_DIR="${RELEASE_ROOT}/.${RELEASE_NAME}.tmp.$$"

if [[ ! "$RELEASE_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "release name contains unsupported characters: $RELEASE_NAME" >&2
  exit 2
fi
if [[ -e "$FINAL_DIR" ]]; then
  echo "release already exists: $FINAL_DIR" >&2
  exit 2
fi

mkdir -p "$RELEASE_ROOT"
trap 'rm -rf "$TEMP_DIR"' EXIT
mkdir -p "$TEMP_DIR"

rsync -a \
  --exclude '/.git/' \
  --exclude '/.venv/' \
  --exclude '/.env' \
  --exclude '/.env.bak*' \
  --exclude '/.local/' \
  --exclude '/.artifacts/' \
  --exclude '/build/' \
  --exclude '/data/' \
  --exclude '/reports/' \
  --exclude '/ui/node_modules/' \
  --exclude '/ui/.vite/' \
  --exclude '/dev.db' \
  --exclude '*.sqlite' \
  --exclude '*.sqlite-*' \
  "$SOURCE_ROOT/" "$TEMP_DIR/"

for mutable_name in .local .artifacts build data reports; do
  mkdir -p "$SOURCE_ROOT/$mutable_name"
  ln -s "$SOURCE_ROOT/$mutable_name" "$TEMP_DIR/$mutable_name"
done
ln -s "$SOURCE_ROOT/.env" "$TEMP_DIR/.env"

source_commit="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
alembic_revision="$(
  cd "$SOURCE_ROOT"
  "$PYTHON_BIN" -m alembic heads | awk 'NR == 1 {print $1}'
)"
source_dirty=false
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
  source_dirty=true
fi
content_sha256="$({
  cd "$TEMP_DIR"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
} | sha256sum | awk '{print $1}')"
cat >"$TEMP_DIR/release-manifest.json" <<EOF
{
  "release_name": "$RELEASE_NAME",
  "built_at": "$(date -Is)",
  "source_commit": "$source_commit",
  "alembic_revision": "$alembic_revision",
  "source_dirty": $source_dirty,
  "content_sha256": "$content_sha256"
}
EOF

find "$TEMP_DIR" -type f -exec chmod a-w {} +
mv "$TEMP_DIR" "$FINAL_DIR"
trap - EXIT
echo "$FINAL_DIR"
