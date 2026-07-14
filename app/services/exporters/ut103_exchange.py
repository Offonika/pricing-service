from __future__ import annotations

import os
import re
from pathlib import Path

UT103_EXCHANGE_ROOT_ENV = "UT103_EXCHANGE_ROOT"
DEFAULT_WINDOWS_UT103_EXCHANGE_ROOT = r"E:\MMExchange\UT103"
WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")
DEFAULT_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
UT103_ENV_KEYS = {
    UT103_EXCHANGE_ROOT_ENV,
    "UT103_FORECAST_SOURCE",
    "UT103_NOMENCLATURE_PROPERTIES_SOURCE",
    "UT103_CUSTOMER_PRICE_TYPES_SOURCE",
    "UT103_PROCUREMENT_ORDERS_SOURCE",
}


def load_ut103_env_file(env_file: str | Path | None = None) -> None:
    """Load only UT103-related keys from the project .env file if present."""

    path = Path(env_file) if env_file is not None else DEFAULT_ENV_FILE
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in UT103_ENV_KEYS:
            continue
        os.environ.setdefault(name, _strip_env_quotes(value.strip()))


def resolve_ut103_exchange_root(explicit: str | Path | None = None) -> str:
    """Resolve the shared UT 10.3 file-exchange root.

    The observed Windows test contour uses E:\\MMExchange\\UT103. Linux
    deployments must set UT103_EXCHANGE_ROOT to a mounted/share path that points
    to the same exchange folder.
    """

    if explicit is not None and str(explicit).strip():
        return _validate_platform_path(str(explicit))

    env_value = os.environ.get(UT103_EXCHANGE_ROOT_ENV)
    if env_value and env_value.strip():
        return _validate_platform_path(env_value.strip())

    if os.name == "nt":
        return DEFAULT_WINDOWS_UT103_EXCHANGE_ROOT

    raise ValueError(
        "Set --exchange-root or UT103_EXCHANGE_ROOT. "
        f"Observed Windows UT 10.3 root: {DEFAULT_WINDOWS_UT103_EXCHANGE_ROOT}"
    )


def _validate_platform_path(path: str) -> str:
    if os.name != "nt" and WINDOWS_DRIVE_PATH_RE.match(path):
        raise ValueError(
            f"{UT103_EXCHANGE_ROOT_ENV} points to Windows path {path}. "
            "Mount/share that folder and use its Linux path instead."
        )
    return path


def _strip_env_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value
