#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${PRICING_SERVICE_SOURCE_ROOT:-/opt/MM/pricing-service}"
RELEASE_ROOT="${PRICING_SERVICE_RELEASE_ROOT:-/opt/MM/releases/pricing-service}"
BASE_RELEASE="${PRICING_SERVICE_BASE_RELEASE:-}"
OVERLAY_PATHS="${PRICING_SERVICE_RELEASE_OVERLAY_PATHS:-}"
ALLOW_OVERLAY="${PRICING_SERVICE_ALLOW_OVERLAY:-0}"
BUILD_UI="${PRICING_SERVICE_BUILD_UI:-1}"
INSTALL_VENV="${PRICING_SERVICE_INSTALL_VENV:-1}"
PYTHON_BOOTSTRAP="${PRICING_SERVICE_PYTHON_BOOTSTRAP:-python3}"
RUNTIME_ENV_FILE="${PRICING_SERVICE_RUNTIME_ENV_FILE:-$SOURCE_ROOT/.env}"
MUTABLE_ROOT="${PRICING_SERVICE_MUTABLE_ROOT:-}"
REQUIRED_BASE_REF="${PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF:-}"
RELEASE_NAME="${1:-architecture-hardening-$(date +%Y%m%d-%H%M%S)}"
FINAL_DIR="${RELEASE_ROOT}/${RELEASE_NAME}"
TEMP_DIR="${RELEASE_ROOT}/.${RELEASE_NAME}.tmp.$$"

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

if [[ ! "$RELEASE_NAME" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "release name contains unsupported characters: $RELEASE_NAME" >&2
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
if [[ -z "$REQUIRED_BASE_REF" ]]; then
  echo "PRICING_SERVICE_RELEASE_REQUIRED_BASE_REF is required" >&2
  exit 2
fi
if [[ -z "$MUTABLE_ROOT" ]]; then
  echo "PRICING_SERVICE_MUTABLE_ROOT is required" >&2
  exit 2
fi
if [[ "$MUTABLE_ROOT" != /* ]]; then
  echo "mutable root must be an absolute path: $MUTABLE_ROOT" >&2
  exit 2
fi
if [[ ! -d "$MUTABLE_ROOT" ]]; then
  echo "mutable root must be an existing persistent directory: $MUTABLE_ROOT" >&2
  exit 2
fi

source_root_real="$(realpath "$SOURCE_ROOT")"
release_root_real="$(realpath -m "$RELEASE_ROOT")"
mutable_root_real="$(realpath "$MUTABLE_ROOT")"
case "$mutable_root_real/" in
  "$source_root_real/"*)
    echo "mutable root must not be the source worktree or live inside it: $MUTABLE_ROOT" >&2
    exit 2
    ;;
  "$release_root_real/"*)
    echo "mutable root must not live inside the immutable release root: $MUTABLE_ROOT" >&2
    exit 2
    ;;
esac
MUTABLE_ROOT="$mutable_root_real"

source_commit="$(git -C "$SOURCE_ROOT" rev-parse HEAD)"
if ! required_base_commit="$(git -C "$SOURCE_ROOT" rev-parse --verify "${REQUIRED_BASE_REF}^{commit}")"; then
  echo "required production base ref cannot be resolved: $REQUIRED_BASE_REF" >&2
  exit 2
fi
if ! git -C "$SOURCE_ROOT" merge-base --is-ancestor "$required_base_commit" "$source_commit"; then
  echo "required production base is not an ancestor of the release candidate: $required_base_commit" >&2
  exit 2
fi
source_dirty=false
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
  source_dirty=true
fi
if [[ "$source_dirty" == true ]]; then
  echo "source checkout is dirty; build from a clean detached worktree" >&2
  exit 2
fi
if [[ -n "$BASE_RELEASE" ]] && [[ "$ALLOW_OVERLAY" != 1 ]]; then
  echo "overlay releases are disabled; build all assets from one clean commit" >&2
  exit 2
fi

if [[ "$BUILD_UI" == 1 ]] && [[ -f "$SOURCE_ROOT/ui/package-lock.json" ]]; then
  npm --prefix "$SOURCE_ROOT/ui" ci
  npm --prefix "$SOURCE_ROOT/ui" run build
fi
if [[ -n "$(git -C "$SOURCE_ROOT" status --porcelain)" ]]; then
  echo "source checkout changed during build; commit deterministic build inputs first" >&2
  exit 2
fi
if [[ ! -f "$SOURCE_ROOT/ui/dist/index.html" ]]; then
  echo "ui/dist/index.html is required; build the UI from the release commit" >&2
  exit 2
fi

mkdir -p "$RELEASE_ROOT"
trap 'rm -rf "$TEMP_DIR"' EXIT
mkdir -p "$TEMP_DIR"

rsync_excludes=(
  --exclude '/.git/'
  --exclude '/.venv/'
  --exclude '/.env'
  --exclude '/.env.bak*'
  --exclude '/.local/'
  --exclude '/.artifacts/'
  --exclude '/build/'
  --exclude '/data/'
  --exclude '/reports/'
  --exclude '/ui/node_modules/'
  --exclude '/ui/.vite/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '*.pyo'
  --exclude '/dev.db'
  --exclude '*.sqlite'
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
  if [[ " $OVERLAY_PATHS " == *" ui/dist/index.html "* ]]; then
    rm -rf "$TEMP_DIR/ui/dist/assets"
    mkdir -p "$TEMP_DIR/ui/dist/assets"
  fi
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

if [[ "$INSTALL_VENV" == 1 ]]; then
  "$PYTHON_BOOTSTRAP" -m venv "$TEMP_DIR/.venv"
  PYTHONDONTWRITEBYTECODE=1 "$TEMP_DIR/.venv/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-compile \
    --require-hashes \
    -r "$TEMP_DIR/requirements.lock"
else
  mkdir -p "$TEMP_DIR/.venv/bin"
  ln -s "$(command -v "$PYTHON_BOOTSTRAP")" "$TEMP_DIR/.venv/bin/python"
fi

for mutable_name in .local .artifacts build data reports; do
  rm -rf "$TEMP_DIR/$mutable_name"
  mkdir -p "$MUTABLE_ROOT/$mutable_name"
  ln -s "$MUTABLE_ROOT/$mutable_name" "$TEMP_DIR/$mutable_name"
done
rm -f "$TEMP_DIR/.env"
ln -s "$RUNTIME_ENV_FILE" "$TEMP_DIR/.env"

# Console scripts created inside the temporary venv embed its absolute path in
# their shebang. Rewrite it before the atomic move so tools such as `alembic`
# remain executable from the final immutable release directory.
while IFS= read -r -d '' console_script; do
  if head -n 1 "$console_script" | grep -Fq "#!$TEMP_DIR/.venv/bin/python"; then
    sed -i "1s|^#!$TEMP_DIR/.venv/bin/python.*$|#!$FINAL_DIR/.venv/bin/python|" \
      "$console_script"
  fi
done < <(find "$TEMP_DIR/.venv/bin" -maxdepth 1 -type f -print0)

python_version="$(PYTHONDONTWRITEBYTECODE=1 "$TEMP_DIR/.venv/bin/python" -c 'import platform; print(platform.python_version())')"
requirements_lock_sha256="$(sha256sum "$TEMP_DIR/requirements.lock" | awk '{print $1}')"
pip_freeze_sha256="$(PYTHONDONTWRITEBYTECODE=1 "$TEMP_DIR/.venv/bin/python" -m pip freeze --all | sha256sum | awk '{print $1}')"
ui_asset_sha256="$({
  cd "$TEMP_DIR/ui/dist"
  find . -type f -print0 | sort -z | xargs -0 sha256sum
} | sha256sum | awk '{print $1}')"
alembic_revision="$(PYTHONDONTWRITEBYTECODE=1 "$TEMP_DIR/.venv/bin/python" - "$TEMP_DIR/alembic/versions" <<'PY'
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
        if target is None or value is None or target.id not in {"revision", "down_revision"}:
            continue
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
content_sha256="$(hash_release_content "$TEMP_DIR")"

cat >"$TEMP_DIR/release-manifest.json" <<EOF
{
  "release_name": "$RELEASE_NAME",
  "built_at": "$(date -Is)",
  "base_release": "${BASE_RELEASE:-source-tree}",
  "overlay_paths": "${OVERLAY_PATHS:-all}",
  "source_commit": "$source_commit",
  "source_verified": true,
  "required_base_ref": "$REQUIRED_BASE_REF",
  "required_base_commit": "$required_base_commit",
  "mutable_root": "$MUTABLE_ROOT",
  "source_dirty": $source_dirty,
  "runtime_env_file": "$RUNTIME_ENV_FILE",
  "python_version": "$python_version",
  "requirements_lock_sha256": "$requirements_lock_sha256",
  "pip_freeze_sha256": "$pip_freeze_sha256",
  "ui_asset_sha256": "$ui_asset_sha256",
  "alembic_revision": "$alembic_revision",
  "content_hash_scheme": "sha256-files-v2-no-python-cache",
  "content_sha256": "$content_sha256"
}
EOF

find "$TEMP_DIR" -type d -exec chmod a+rX {} +
find "$TEMP_DIR" -type f -exec chmod a-w {} +
mv "$TEMP_DIR" "$FINAL_DIR"
trap - EXIT
echo "$FINAL_DIR"
