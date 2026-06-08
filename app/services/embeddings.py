from __future__ import annotations

from collections.abc import Iterable

import httpx
from openai import OpenAI

from app.core.config import get_settings


class EmbeddingClient:
    def __init__(self, model: str | None = None, batch_size: int | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.embeddings_model
        self.batch_size = batch_size or settings.embeddings_batch_size
        self._http_client = (
            httpx.Client(proxy=settings.openai_http_proxy, timeout=60.0)
            if settings.openai_http_proxy
            else None
        )
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
            http_client=self._http_client,
        )

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        payload = [t if t is not None else "" for t in texts]
        if not payload:
            return []
        response = self.client.embeddings.create(model=self.model, input=payload)
        return [item.embedding for item in response.data]

    def preflight(self, *, expected_dim: int | None = None) -> int:
        embeddings = self.embed_texts(["pricing-service embeddings preflight"])
        if not embeddings:
            raise RuntimeError("embedding preflight returned no vectors")
        dim = len(embeddings[0])
        if expected_dim is not None and dim != expected_dim:
            raise RuntimeError(f"embedding dim mismatch: expected {expected_dim}, got {dim}")
        return dim

    def close(self) -> None:
        if self._http_client is not None:
            self._http_client.close()
