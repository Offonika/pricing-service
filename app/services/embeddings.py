from __future__ import annotations

from collections.abc import Iterable

from openai import OpenAI

from app.core.config import get_settings


class EmbeddingClient:
    def __init__(self, model: str | None = None, batch_size: int | None = None) -> None:
        settings = get_settings()
        self.model = model or settings.embeddings_model
        self.batch_size = batch_size or settings.embeddings_batch_size
        self.client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_api_base,
        )

    def embed_texts(self, texts: Iterable[str]) -> list[list[float]]:
        payload = [t if t is not None else "" for t in texts]
        if not payload:
            return []
        response = self.client.embeddings.create(model=self.model, input=payload)
        return [item.embedding for item in response.data]
