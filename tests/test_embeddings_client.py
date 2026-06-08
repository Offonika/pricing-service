from __future__ import annotations

from types import SimpleNamespace

from app.services import embeddings as embeddings_module


def test_embedding_client_passes_configured_openai_proxy(monkeypatch):
    captured: dict[str, object] = {}

    class DummyHttpClient:
        def __init__(self, *, proxy: str, timeout: float):
            self.proxy = proxy
            self.timeout = timeout
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class DummyOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    proxy_url = "http://user:password@proxy-host:3128"
    monkeypatch.setattr(
        embeddings_module,
        "get_settings",
        lambda: SimpleNamespace(
            embeddings_model="text-embedding-3-small",
            embeddings_batch_size=64,
            openai_api_key="test-key",
            openai_api_base="https://api.openai.com/v1",
            openai_http_proxy=proxy_url,
        ),
    )
    monkeypatch.setattr(embeddings_module.httpx, "Client", DummyHttpClient)
    monkeypatch.setattr(embeddings_module, "OpenAI", DummyOpenAI)

    client = embeddings_module.EmbeddingClient()

    assert captured["http_client"].proxy == proxy_url
    assert captured["http_client"].timeout == 60.0
    client.close()
    assert captured["http_client"].closed is True
