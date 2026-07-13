#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${PRICING_SERVICE_SOURCE_ROOT:-/opt/MM/pricing-service}"
RELEASE_ROOT="${PRICING_SERVICE_RELEASE_ROOT:-/opt/MM/releases/pricing-service}"
BASE_RELEASE="${PRICING_SERVICE_BASE_RELEASE:-}"
OVERLAY_PATHS="${PRICING_SERVICE_RELEASE_OVERLAY_PATHS:-}"
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

rsync_excludes=(
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
  --exclude '*.sqlite-*'
)

if [[ -n "$BASE_RELEASE" ]]; then
  if [[ ! -d "$BASE_RELEASE" ]]; then
    echo "base release does not exist: $BASE_RELEASE" >&2
    exit 2
  fi
  if [[ -z "$OVERLAY_PATHS" ]]; then
    echo "PRICING_SERVICE_RELEASE_OVERLAY_PATHS is required with a base release" >&2
    exit 2
  fi
  rsync -a "${rsync_excludes[@]}" \
    --exclude '/release-manifest.json' \
    "$BASE_RELEASE/" "$TEMP_DIR/"
  for relative_path in $OVERLAY_PATHS; do
    if [[ "$relative_path" = /* || "$relative_path" == *".."* ]]; then
      echo "unsupported overlay path: $relative_path" >&2
      exit 2
    fi
    if [[ ! -f "$SOURCE_ROOT/$relative_path" ]]; then
      echo "overlay file does not exist: $SOURCE_ROOT/$relative_path" >&2
      exit 2
    fi
    mkdir -p "$TEMP_DIR/$(dirname "$relative_path")"
    rsync -a "$SOURCE_ROOT/$relative_path" "$TEMP_DIR/$relative_path"
  done
else
  rsync -a "${rsync_excludes[@]}" "$SOURCE_ROOT/" "$TEMP_DIR/"
fi

for mutable_name in .local .artifacts build data reports; do
  rm -rf "$TEMP_DIR/$mutable_name"
  mkdir -p "$SOURCE_ROOT/$mutable_name"
  ln -s "$SOURCE_ROOT/$mutable_name" "$TEMP_DIR/$mutable_name"
done
rm -f "$TEMP_DIR/.env"
ln -s "$SOURCE_ROOT/.env" "$TEMP_DIR/.env"

source_commit="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
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
  "base_release": "${BASE_RELEASE:-source-tree}",
  "overlay_paths": "${OVERLAY_PATHS:-all}",
  "source_commit": "$source_commit",
  "source_dirty": $source_dirty,
  "content_sha256": "$content_sha256"
}
EOF

find "$TEMP_DIR" -type f -exec chmod a-w {} +
mv "$TEMP_DIR" "$FINAL_DIR"
trap - EXIT
echo "$FINAL_DIR"
