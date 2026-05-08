#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


DEFAULT_VENV_DIR = "~/.cache/offline-asr-review/venv"
DEFAULT_HF_HOME = "~/.cache/huggingface"


def expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def resolve_venv_dir(env: dict[str, str] | None = None) -> str:
    scope = env or os.environ
    return expand((scope.get("OFFLINE_ASR_VENV") or DEFAULT_VENV_DIR).strip())


def resolve_python_bin(env: dict[str, str] | None = None) -> str:
    scope = env or os.environ
    explicit = (scope.get("OFFLINE_ASR_PYTHON") or "").strip()
    if explicit:
        return expand(explicit)
    return os.path.join(resolve_venv_dir(scope), "bin", "python3")


def resolve_hf_home(env: dict[str, str] | None = None) -> str:
    scope = env or os.environ
    if (scope.get("HF_HOME") or "").strip():
        return expand(scope["HF_HOME"])
    if (scope.get("HUGGINGFACE_HUB_CACHE") or "").strip():
        return expand(Path(scope["HUGGINGFACE_HUB_CACHE"]).parent.as_posix())
    if (scope.get("XDG_CACHE_HOME") or "").strip():
        return expand(os.path.join(scope["XDG_CACHE_HOME"], "huggingface"))
    return expand(DEFAULT_HF_HOME)


def resolve_hf_hub_dir(env: dict[str, str] | None = None) -> str:
    scope = env or os.environ
    explicit = (scope.get("HUGGINGFACE_HUB_CACHE") or "").strip()
    if explicit:
        return expand(explicit)
    return os.path.join(resolve_hf_home(scope), "hub")


def model_repo_id(model_name: str) -> str | None:
    model = (model_name or "").strip()
    if not model:
        return None
    expanded = Path(model).expanduser()
    if (
        model.startswith(".")
        or model.startswith("~")
        or os.path.isabs(model)
        or os.path.sep in model
        or expanded.exists()
    ):
        return None
    if "/" in model:
        return model
    if model.startswith("faster-whisper-"):
        return f"Systran/{model}"
    return f"Systran/faster-whisper-{model}"


def model_cache_dir(model_name: str, env: dict[str, str] | None = None) -> Path | None:
    repo_id = model_repo_id(model_name)
    if not repo_id:
        return None
    return Path(resolve_hf_hub_dir(env)) / f"models--{repo_id.replace('/', '--')}"


def describe_exception(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}".strip()


def shorten(text: str, limit: int = 500) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def is_model_cache_error(message: str, model_name: str, env: dict[str, str] | None = None) -> bool:
    text = message or ""
    cache_dir = model_cache_dir(model_name, env)
    if not cache_dir:
        return False
    cache_path = str(cache_dir)
    return (
        ".incomplete" in text
        or cache_path in text
        or cache_dir.name in text
        or "snapshot_download" in text
        or "huggingface" in text
        and "No such file or directory" in text
    )


def repair_model_cache(model_name: str, env: dict[str, str] | None = None) -> str | None:
    cache_dir = model_cache_dir(model_name, env)
    if not cache_dir or not cache_dir.exists():
        return None
    shutil.rmtree(cache_dir)
    return str(cache_dir)


def model_lock_path(model_name: str, env: dict[str, str] | None = None) -> str:
    repo_id = model_repo_id(model_name)
    if repo_id:
        slug = repo_id.replace("/", "--")
    else:
        slug = Path(model_name or "model").expanduser().as_posix().replace("/", "--")
    return os.path.join(tempfile.gettempdir(), f"openclaw-asr-model-{slug}.lock")


@contextlib.contextmanager
def hold_model_lock(model_name: str, env: dict[str, str] | None = None):
    lock_file = model_lock_path(model_name, env)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    with open(lock_file, "a+b") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            if handle.tell() == 0 and handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def run_checked(cmd: list[str], env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if proc.returncode == 0:
        return
    details = shorten(proc.stderr or proc.stdout or "")
    raise RuntimeError(f"venv_bootstrap_failed: {' '.join(cmd[:4])}: {details}")


def ensure_runtime(
    env: dict[str, str] | None = None,
    *,
    require_ffmpeg: bool = False,
) -> str:
    scope = dict(os.environ)
    if env:
        scope.update(env)

    if shutil.which("python3") is None:
        raise RuntimeError("python_missing: python3")

    if require_ffmpeg:
        missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
        if missing:
            raise RuntimeError(f"tool_missing: {', '.join(missing)}")

    python_bin = resolve_python_bin(scope)
    venv_dir = os.path.dirname(os.path.dirname(python_bin))
    created = False
    if not os.path.exists(python_bin):
        run_checked(["python3", "-m", "venv", venv_dir], scope)
        created = True

    import_check = subprocess.run(
        [python_bin, "-c", "import faster_whisper"],
        env=scope,
        text=True,
        capture_output=True,
    )
    if import_check.returncode != 0:
        if created:
            run_checked(
                [
                    python_bin,
                    "-m",
                    "pip",
                    "--disable-pip-version-check",
                    "install",
                    "--upgrade",
                    "pip",
                ],
                scope,
            )
        run_checked(
            [python_bin, "-m", "pip", "--disable-pip-version-check", "install", "faster-whisper"],
            scope,
        )
        run_checked([python_bin, "-c", "import faster_whisper"], scope)

    if not os.path.exists(python_bin):
        raise RuntimeError(f"python_missing: {python_bin}")
    return python_bin


def load_whisper_model_with_repair(
    load_model_fn, model_name: str, env: dict[str, str] | None = None
):
    first_error = None
    with hold_model_lock(model_name, env):
        for attempt in range(2):
            try:
                return load_model_fn()
            except Exception as exc:  # pragma: no cover - exercised through callers/tests
                first_error = exc
                message = describe_exception(exc)
                if attempt == 0 and is_model_cache_error(message, model_name, env):
                    repaired = repair_model_cache(model_name, env)
                    if repaired:
                        print(f"repairing broken model cache: {repaired}", file=sys.stderr)
                    else:
                        raise RuntimeError(f"model_cache_corrupted: {shorten(message)}") from exc
                    continue
                if is_model_cache_error(message, model_name, env):
                    raise RuntimeError(f"model_download_failed: {shorten(message)}") from exc
                raise
    if first_error is not None:  # pragma: no cover - defensive
        raise first_error
    raise RuntimeError("model_download_failed: unknown_error")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared ASR runtime helper.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure_parser = subparsers.add_parser(
        "ensure-runtime", help="Ensure ASR venv exists and print python path."
    )
    ensure_parser.add_argument(
        "--require-ffmpeg", action="store_true", help="Also require ffmpeg and ffprobe."
    )
    ensure_parser.add_argument(
        "--print-python", action="store_true", help="Print resolved python path."
    )

    repair_parser = subparsers.add_parser(
        "repair-model-cache", help="Remove one broken HF model cache."
    )
    repair_parser.add_argument(
        "--model", required=True, help="Model name, for example small or large-v3."
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "ensure-runtime":
            python_bin = ensure_runtime(require_ffmpeg=args.require_ffmpeg)
            if args.print_python:
                print(python_bin)
            return 0
        if args.command == "repair-model-cache":
            repaired = repair_model_cache(args.model)
            if repaired:
                print(repaired)
            return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
