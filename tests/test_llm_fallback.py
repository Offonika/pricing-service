from __future__ import annotations

import httpx

from app.services.llm_fallback import FallbackChatClient, LlmProvider


class _FakeResponse:
    def __init__(self, payload: dict | None = None, *, status_code: int = 200) -> None:
        self.payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed",
                request=httpx.Request("POST", "http://example.test"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict:
        return self.payload


class _FakeHttpClient:
    def __init__(self, responses: list[_FakeResponse | Exception]) -> None:
        self.responses = responses

    def post(self, *args, **kwargs):  # noqa: ANN002, ANN003
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        return None


def test_fallback_chat_client_uses_openai_after_local_failure(monkeypatch) -> None:
    client = FallbackChatClient(
        providers=[
            LlmProvider("local", "http://127.0.0.1:1234", "local-model"),
            LlmProvider("openai", "https://proxy.example/v1", "gpt-4o-mini", "secret"),
        ]
    )
    fake = _FakeHttpClient(
        [
            httpx.ConnectError("offline"),
            _FakeResponse({"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ]
    )
    monkeypatch.setattr(client, "_client_for", lambda provider: fake)

    result = client.chat_completion(messages=[{"role": "user", "content": "ping"}])

    assert result.provider == "openai"
    assert result.model == "gpt-4o-mini"
    assert result.content == '{"ok": true}'


def test_provider_endpoint_accepts_base_with_or_without_v1() -> None:
    assert (
        LlmProvider("openai", "https://proxy.example/v1", "model").endpoint
        == "https://proxy.example/v1/chat/completions"
    )
    assert (
        LlmProvider("local", "http://127.0.0.1:1234", "model").endpoint
        == "http://127.0.0.1:1234/v1/chat/completions"
    )


def test_fallback_chat_client_tries_next_provider_on_invalid_content(monkeypatch) -> None:
    client = FallbackChatClient(
        providers=[
            LlmProvider("local", "http://127.0.0.1:1234", "local-model"),
            LlmProvider("openai", "https://proxy.example/v1", "gpt-4o-mini", "secret"),
        ]
    )
    fake = _FakeHttpClient(
        [
            _FakeResponse({"choices": [{"message": {"content": "not-json"}}]}),
            _FakeResponse({"choices": [{"message": {"content": '{"ok": true}'}}]}),
        ]
    )
    monkeypatch.setattr(client, "_client_for", lambda provider: fake)

    result = client.chat_completion(
        messages=[{"role": "user", "content": "ping"}],
        response_validator=lambda content: content.startswith("{"),
    )

    assert result.provider == "openai"
    assert result.content == '{"ok": true}'
