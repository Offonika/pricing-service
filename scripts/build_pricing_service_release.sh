#!/usr/bin/env bash
set -euo pipefail

export PYTHONDONTWRITEBYTECODE=1

SOURCE_ROOT="${PRICING_SERVICE_SOURCE_ROOT:-/opt/MM/pricing-service}"
RUNTIME_ROOT="${PRICING_SERVICE_RUNTIME_ROOT:-/opt/MM/pricing-service}"
RELEASE_ROOT="${PRICING_SERVICE_RELEASE_ROOT:-/opt/MM/releases/pricing-service}"
BASE_RELEASE="${PRICING_SERVICE_BASE_RELEASE:-}"
OVERLAY_PATHS="${PRICING_SERVICE_RELEASE_OVERLAY_PATHS:-}"
PYTHON_BIN="${PRICING_SERVICE_PYTHON_BIN:-/opt/MM/pricing-service/.venv/bin/python}"
PYTHON_BOOTSTRAP="${PRICING_SERVICE_PYTHON_BOOTSTRAP:-python3}"
ALEMBIC_REVISION="${PRICING_SERVICE_ALEMBIC_REVISION:-}"
ALLOW_OVERLAY="${PRICING_SERVICE_ALLOW_OVERLAY:-0}"
BUILD_UI="${PRICING_SERVICE_BUILD_UI:-1}"
INSTALL_VENV="${PRICING_SERVICE_INSTALL_VENV:-1}"
RUNTIME_ENV_FILE="${PRICING_SERVICE_RUNTIME_ENV_FILE:-$RUNTIME_ROOT/.env}"
RELEASE_NAME="${1:-architecture-hardening-$(date +%Y%m%d-%H%M%S)}"
FINAL_DIR="${RELEASE_ROOT}/${RELEASE_NAME}"
TEMP_DIR="${RELEASE_ROOT}/.${RELEASE_NAME}.tmp.$$"

if [[ ! "$RELEASE_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "release name contains unsupported characters: $RELEASE_NAME" >&2
  exit 2
fi
if [[ ! -d "$SOURCE_ROOT/.git" && ! -f "$SOURCE_ROOT/.git" ]]; then
  echo "release source is not a Git worktree: $SOURCE_ROOT" >&2
  exit 2
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain --untracked-files=all)" ]]; then
  echo "release source must have a clean Git tree: $SOURCE_ROOT" >&2
  exit 2
fi
if [[ -e "$FINAL_DIR" ]]; then
  echo "release already exists: $FINAL_DIR" >&2
  exit 2
fi
if [[ ! -f "$SOURCE_ROOT/requirements.lock" ]]; then
  echo "requirements.lock is required for an immutable release" >&2
  exit 2
fi
if [[ ! -f "$RUNTIME_ENV_FILE" ]]; then
  echo "runtime env file is missing: $RUNTIME_ENV_FILE" >&2
  exit 2
fi
if [[ -n "$BASE_RELEASE" ]] && [[ "$ALLOW_OVERLAY" != 1 ]]; then
  echo "overlay releases require PRICING_SERVICE_ALLOW_OVERLAY=1" >&2
  exit 2
fi

source_commit="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"

if [[ "$BUILD_UI" == 1 ]] && [[ -f "$SOURCE_ROOT/ui/package-lock.json" ]]; then
  npm --prefix "$SOURCE_ROOT/ui" ci
  npm --prefix "$SOURCE_ROOT/ui" run build
fi
if [[ ! -f "$SOURCE_ROOT/ui/dist/index.html" ]]; then
  echo "ui/dist/index.html is required; build the UI from the release commit" >&2
  exit 2
fi

mkdir -p "$RELEASE_ROOT"
trap 'rm -rf "$TEMP_DIR"' EXIT
mkdir -p "$TEMP_DIR"

rsync_excludes=(
  --exclude '/.git'
  --exclude '/.git/'
  --exclude '/.venv/'
  --exclude '/.env'
  --exclude '/.env.bak*'
  --exclude '/.local/'
  --exclude '/.artifacts/'
  --exclude '/build/'
  --exclude '/data/'
  --exclude '/embeddings/'
  --exclude '/reports/'
  --exclude '/ui/node_modules/'
  --exclude '/ui/.vite/'
  --exclude '/dev.db'
  --exclude '*.sqlite'
  --exclude '*.sqlite-*'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude '.ruff_cache/'
  --exclude '*.pyc'
)

base_release_value="source-tree"
if [[ -n "$BASE_RELEASE" ]]; then
  if [[ ! -d "$BASE_RELEASE" ]]; then
    echo "base release does not exist: $BASE_RELEASE" >&2
    exit 2
  fi
  base_release_manifest="$BASE_RELEASE/release-manifest.json"
  if [[ ! -f "$base_release_manifest" ]]; then
    echo "base release manifest is missing: $base_release_manifest" >&2
    exit 2
  fi
  if ! "$PYTHON_BIN" - "$base_release_manifest" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if manifest.get("source_dirty") is not False:
    raise SystemExit(1)
PY
  then
    echo "overlay base release must have source_dirty=false: $BASE_RELEASE" >&2
    exit 2
  fi
  if [[ -z "$OVERLAY_PATHS" ]]; then
    echo "PRICING_SERVICE_RELEASE_OVERLAY_PATHS is required with a base release" >&2
    exit 2
  fi

  read -r -a overlay_path_list <<<"$OVERLAY_PATHS"
  ui_overlay=false
  for relative_path in "${overlay_path_list[@]}"; do
    if [[ "$relative_path" = /* || "/$relative_path/" == *"/../"* ]]; then
      echo "unsupported overlay path: $relative_path" >&2
      exit 2
    fi
    if [[ "$relative_path" == *"__pycache__"* || "$relative_path" == *.pyc ]]; then
      echo "cache files cannot be overlaid: $relative_path" >&2
      exit 2
    fi
    if [[ ! -f "$SOURCE_ROOT/$relative_path" ]]; then
      echo "overlay file does not exist: $SOURCE_ROOT/$relative_path" >&2
      exit 2
    fi
    if [[ "$relative_path" == ui/dist/* ]]; then
      ui_overlay=true
    fi
  done

  rsync -a "${rsync_excludes[@]}" \
    --exclude '/release-manifest.json' \
    "$BASE_RELEASE/" "$TEMP_DIR/"
  find "$TEMP_DIR" -type d -exec chmod u+w {} +
  if [[ "$ui_overlay" == true ]]; then
    rm -rf "$TEMP_DIR/ui/dist/assets"
    mkdir -p "$TEMP_DIR/ui/dist/assets"
  fi
  for relative_path in "${overlay_path_list[@]}"; do
    mkdir -p "$TEMP_DIR/$(dirname "$relative_path")"
    rsync -a "$SOURCE_ROOT/$relative_path" "$TEMP_DIR/$relative_path"
  done
  base_release_value="$(readlink -f "$BASE_RELEASE")"
else
  rsync -a "${rsync_excludes[@]}" "$SOURCE_ROOT/" "$TEMP_DIR/"
fi

if [[ "$INSTALL_VENV" == 1 ]]; then
  "$PYTHON_BOOTSTRAP" -m venv "$TEMP_DIR/.venv"
  "$TEMP_DIR/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --require-hashes \
    -r "$TEMP_DIR/requirements.lock"
else
  mkdir -p "$TEMP_DIR/.venv/bin"
  ln -s "$(command -v "$PYTHON_BOOTSTRAP")" "$TEMP_DIR/.venv/bin/python"
fi

for mutable_name in .local .artifacts build data embeddings reports; do
  rm -rf "$TEMP_DIR/$mutable_name"
  mkdir -p "$RUNTIME_ROOT/$mutable_name"
  ln -s "$RUNTIME_ROOT/$mutable_name" "$TEMP_DIR/$mutable_name"
done
rm -f "$TEMP_DIR/.env"
ln -s "$RUNTIME_ENV_FILE" "$TEMP_DIR/.env"

if [[ -f "$TEMP_DIR/app/main.py" ]]; then
  (
    cd "$TEMP_DIR"
    PYTHONPATH="$TEMP_DIR" "$TEMP_DIR/.venv/bin/python" -c "import app.main"
  )
fi

if [[ -z "$ALEMBIC_REVISION" ]]; then
  ALEMBIC_REVISION="$("$TEMP_DIR/.venv/bin/python" - "$TEMP_DIR/alembic/versions" <<'PY'
import ast
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
revisions = set()
parents = set()
for path in root.glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        target = None
        value = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target
            value = node.value
        if target is not None and target.id in {"revision", "down_revision"}:
            try:
                values[target.id] = ast.literal_eval(value)
            except (TypeError, ValueError):
                pass
    revision = values.get("revision")
    down_revision = values.get("down_revision")
    if isinstance(revision, str):
        revisions.add(revision)
    if isinstance(down_revision, str):
        parents.add(down_revision)
    elif isinstance(down_revision, (tuple, list)):
        parents.update(item for item in down_revision if isinstance(item, str))
heads = sorted(revisions - parents)
if len(heads) != 1:
    raise SystemExit(f"expected one Alembic head, found: {heads}")
print(heads[0])
PY
)"
fi
if [[ -z "$ALEMBIC_REVISION" ]]; then
  echo "could not resolve Alembic revision" >&2
  exit 2
fi

python_version="$("$TEMP_DIR/.venv/bin/python" -c 'import platform; print(platform.python_version())')"
requirements_lock_sha256="$(sha256sum "$TEMP_DIR/requirements.lock" | awk '{print $1}')"
pip_freeze_sha256="$("$TEMP_DIR/.venv/bin/python" -m pip freeze --all | sha256sum | awk '{print $1}')"
ui_asset_sha256="$({
  cd "$TEMP_DIR/ui/dist"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
} | sha256sum | awk '{print $1}')"
content_sha256="$({
  cd "$TEMP_DIR"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
} | sha256sum | awk '{print $1}')"

built_at="$(date -Is)"
"$TEMP_DIR/.venv/bin/python" - \
  "$TEMP_DIR/release-manifest.json" \
  "$RELEASE_NAME" \
  "$built_at" \
  "$base_release_value" \
  "${OVERLAY_PATHS:-all}" \
  "$source_commit" \
  "$RUNTIME_ENV_FILE" \
  "$python_version" \
  "$requirements_lock_sha256" \
  "$pip_freeze_sha256" \
  "$ui_asset_sha256" \
  "$ALEMBIC_REVISION" \
  "$content_sha256" <<'PY'
import json
import sys
from pathlib import Path

(
    manifest_path,
    release_name,
    built_at,
    base_release,
    overlay_paths,
    source_commit,
    runtime_env_file,
    python_version,
    requirements_lock_sha256,
    pip_freeze_sha256,
    ui_asset_sha256,
    alembic_revision,
    content_sha256,
) = sys.argv[1:]
payload = {
    "release_name": release_name,
    "built_at": built_at,
    "base_release": base_release,
    "overlay_paths": overlay_paths,
    "source_commit": source_commit,
    "source_dirty": False,
    "runtime_env_file": runtime_env_file,
    "python_version": python_version,
    "requirements_lock_sha256": requirements_lock_sha256,
    "pip_freeze_sha256": pip_freeze_sha256,
    "ui_asset_sha256": ui_asset_sha256,
    "alembic_revision": alembic_revision,
    "content_sha256": content_sha256,
}
Path(manifest_path).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

find "$TEMP_DIR" -type d -exec chmod a+rX {} +
find "$TEMP_DIR" -type f -exec chmod a-w {} +
find "$TEMP_DIR" -type d -exec chmod a-w {} +
mv "$TEMP_DIR" "$FINAL_DIR"
trap - EXIT
echo "$FINAL_DIR"
