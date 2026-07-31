from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import get_settings
from app.schemas.executive_dashboard import ExecutiveInstrumentsResponse

_FORBIDDEN_KEYS = {
    "access_method",
    "connection_string",
    "dns_aliases",
    "fingerprint",
    "ip",
    "management_endpoint_ref",
    "password",
    "port",
    "private_key",
    "public_host",
    "secret",
    "secret_ref",
    "ssh_alias",
    "ssh_aliases",
    "token",
    "username",
    "webhook",
}
_FORBIDDEN_KEY_TOKENS = {
    "alias",
    "aliases",
    "connection",
    "endpoint",
    "fingerprint",
    "ip",
    "ipv4",
    "ipv6",
    "password",
    "port",
    "secret",
    "token",
    "username",
    "webhook",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"/rest/\d+/[A-Za-z0-9_-]{12,}(?:/|$)"),
    re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2}|:\d{1,5})?\b"),
    re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,}[0-9A-Fa-f:]{1,}\b"),
    re.compile(r"\b(?:localhost|[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+):\d{1,5}\b"),
    re.compile(r"\b[A-Za-z][A-Za-z0-9-]{1,62}:\d{1,5}\b"),
    re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,63}\b", re.IGNORECASE),
    re.compile(r"\bSHA256:[A-Za-z0-9+/=]{16,}\b"),
    re.compile(r"\b(?:ssh-config|secret|token|password|username|login)\s*[:=]", re.IGNORECASE),
    re.compile(r"\b(?:логин|пароль)\s*[:=]", re.IGNORECASE),
)
_KNOWN_TECHNICAL_IDENTIFIERS = {
    "ai-models-win",
    "arsen",
    "asr-win",
    "bitrix-box-diag",
    "mm-ai-models",
    "mm-srv-a4rq4fs",
    "openclaw-b",
    "selectel-dr-01",
    "ut103-1cserv",
}


def _empty_response(
    *, source_status: str, freshness_status: str, note: str
) -> ExecutiveInstrumentsResponse:
    return ExecutiveInstrumentsResponse(
        generated_at=datetime.now(UTC),
        source_status=source_status,
        freshness_status=freshness_status,
        summary={},
        devices=[],
        warnings=[],
        capabilities={
            "access_governance": "read_only",
            "access_mutations": False,
            "network_scanning": False,
        },
        note=note,
    )


def _assert_sanitized(value: Any, *, path: str = "snapshot") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).strip().lower()
            key_tokens = set(re.split(r"[^a-z0-9]+", normalized_key))
            if normalized_key in _FORBIDDEN_KEYS or key_tokens & _FORBIDDEN_KEY_TOKENS:
                raise ValueError(f"forbidden infrastructure snapshot field at {path}")
            _assert_sanitized(item, path=f"{path}.{normalized_key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_sanitized(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ValueError(f"secret-like infrastructure snapshot value at {path}")
        if value.strip().casefold() in _KNOWN_TECHNICAL_IDENTIFIERS and not path.endswith(
            ".device_key"
        ):
            raise ValueError(f"technical identifier in infrastructure snapshot at {path}")


def load_executive_instruments_snapshot(
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> ExecutiveInstrumentsResponse:
    settings = get_settings()
    target = path or Path(settings.executive_dashboard_instruments_snapshot_path)
    if not target.exists():
        return _empty_response(
            source_status="source_missing",
            freshness_status="missing",
            note="Снимок инфраструктуры ещё не опубликован.",
        )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        _assert_sanitized(payload)
        response = ExecutiveInstrumentsResponse.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError):
        return _empty_response(
            source_status="source_error",
            freshness_status="error",
            note="Снимок инфраструктуры повреждён или не прошёл безопасную проверку.",
        )
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    generated_at = response.generated_at
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    if generated_at - current > timedelta(minutes=5):
        return _empty_response(
            source_status="source_error",
            freshness_status="error",
            note="Время снимка инфраструктуры находится в будущем.",
        )
    max_lag = timedelta(
        minutes=max(1, int(settings.executive_dashboard_instruments_max_lag_minutes))
    )
    response.freshness_status = "stale" if current - generated_at > max_lag else "fresh"
    if response.freshness_status == "stale" and response.source_status in {
        "ready",
        "partial",
    }:
        response.source_status = "stale"
    return response
