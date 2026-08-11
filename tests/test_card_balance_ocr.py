from __future__ import annotations

from app.core.config import Settings
from app.services import card_balance_ocr


def test_build_openai_client_uses_explicit_proxy(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_http_client = object()
    fake_openai_client = object()

    def build_http_client(**kwargs):
        captured["httpx_kwargs"] = kwargs
        return fake_http_client

    def build_openai_client(**kwargs):
        captured["openai_kwargs"] = kwargs
        return fake_openai_client

    monkeypatch.setattr(card_balance_ocr.httpx, "Client", build_http_client)
    monkeypatch.setattr(card_balance_ocr, "OpenAI", build_openai_client)

    result = card_balance_ocr._build_openai_client(
        Settings(
            openai_api_key="test-key",
            openai_http_proxy="http://proxy.example:8080",
        )
    )

    assert result is fake_openai_client
    assert captured["httpx_kwargs"] == {"proxy": "http://proxy.example:8080"}
    assert captured["openai_kwargs"] == {
        "api_key": "test-key",
        "base_url": None,
        "http_client": fake_http_client,
    }


def test_build_openai_client_does_not_create_http_client_without_proxy(monkeypatch) -> None:
    captured: dict[str, object] = {}
    fake_openai_client = object()

    def fail_http_client(**kwargs):
        raise AssertionError(f"unexpected explicit HTTP client: {kwargs}")

    def build_openai_client(**kwargs):
        captured["openai_kwargs"] = kwargs
        return fake_openai_client

    monkeypatch.setattr(card_balance_ocr.httpx, "Client", fail_http_client)
    monkeypatch.setattr(card_balance_ocr, "OpenAI", build_openai_client)

    result = card_balance_ocr._build_openai_client(
        Settings(openai_api_key="test-key", openai_http_proxy=None)
    )

    assert result is fake_openai_client
    assert captured["openai_kwargs"] == {
        "api_key": "test-key",
        "base_url": None,
    }
