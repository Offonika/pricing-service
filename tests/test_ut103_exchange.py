from __future__ import annotations

from pathlib import Path

import pytest

from app.services.exporters import ut103_exchange


def test_resolve_ut103_exchange_root_prefers_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UT103_EXCHANGE_ROOT", "/env/root")

    assert ut103_exchange.resolve_ut103_exchange_root("/explicit/root") == "/explicit/root"


def test_resolve_ut103_exchange_root_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UT103_EXCHANGE_ROOT", "/env/root")

    assert ut103_exchange.resolve_ut103_exchange_root() == "/env/root"


def test_resolve_ut103_exchange_root_uses_windows_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UT103_EXCHANGE_ROOT", raising=False)
    monkeypatch.setattr(ut103_exchange.os, "name", "nt")

    assert (
        ut103_exchange.resolve_ut103_exchange_root()
        == ut103_exchange.DEFAULT_WINDOWS_UT103_EXCHANGE_ROOT
    )


def test_resolve_ut103_exchange_root_requires_env_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UT103_EXCHANGE_ROOT", raising=False)
    monkeypatch.setattr(ut103_exchange.os, "name", "posix")

    with pytest.raises(ValueError, match="UT103_EXCHANGE_ROOT"):
        ut103_exchange.resolve_ut103_exchange_root()


def test_resolve_ut103_exchange_root_rejects_windows_path_on_non_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ut103_exchange.os, "name", "posix")

    with pytest.raises(ValueError, match="Mount/share"):
        ut103_exchange.resolve_ut103_exchange_root(r"E:\MMExchange\UT103")


def test_load_ut103_env_file_loads_only_ut103_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "UT103_EXCHANGE_ROOT=/mnt/ut103_exchange/UT103",
                "UT103_FORECAST_SOURCE=custom-forecast",
                "UT103_CUSTOMER_PRICE_TYPES_SOURCE=custom-price-types",
                "DATABASE_URL=postgresql://secret",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("UT103_EXCHANGE_ROOT", raising=False)
    monkeypatch.delenv("UT103_FORECAST_SOURCE", raising=False)
    monkeypatch.delenv("UT103_CUSTOMER_PRICE_TYPES_SOURCE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    ut103_exchange.load_ut103_env_file(env_file)

    assert ut103_exchange.os.environ["UT103_EXCHANGE_ROOT"] == "/mnt/ut103_exchange/UT103"
    assert ut103_exchange.os.environ["UT103_FORECAST_SOURCE"] == "custom-forecast"
    assert ut103_exchange.os.environ["UT103_CUSTOMER_PRICE_TYPES_SOURCE"] == "custom-price-types"
    assert "DATABASE_URL" not in ut103_exchange.os.environ


def test_load_ut103_env_file_keeps_existing_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("UT103_EXCHANGE_ROOT=/mnt/from-file\n", encoding="utf-8")
    monkeypatch.setenv("UT103_EXCHANGE_ROOT", "/mnt/from-process")

    ut103_exchange.load_ut103_env_file(env_file)

    assert ut103_exchange.os.environ["UT103_EXCHANGE_ROOT"] == "/mnt/from-process"
