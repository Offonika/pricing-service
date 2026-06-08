from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings

logger = logging.getLogger("app.services.llm_fallback")

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com"


class LlmFallbackUnavailable(RuntimeError):
    """Raised when no configured LLM provider can serve the request."""


@dataclass(frozen=True)
class LlmProvider:
    name: str
    base_url: str
    model: str
    api_key: str | None = None

    @property
    def endpoint(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            return f"{base_url}/chat/completions"
        return f"{base_url}/v1/chat/completions"

    @property
    def trust_env(self) -> bool:
        host = (urlparse(self.base_url).hostname or "").lower()
        return host not in {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class LlmChatResult:
    content: str
    provider: str
    model: str
    raw: dict[str, Any]


def _clean_mode(value: str | None) -> str:
    mode = (value or "auto").strip().lower()
    if mode in {"local", "openai", "auto", "off"}:
        return mode
    logger.warning("unknown COMPETITOR_MATCHING_LLM_PROVIDER=%s; using auto", mode)
    return "auto"


def configured_llm_providers(mode: str | None = None) -> list[LlmProvider]:
    settings = get_settings()
    selected = _clean_mode(mode or os.environ.get("COMPETITOR_MATCHING_LLM_PROVIDER"))
    if selected == "off":
        return []

    providers: list[LlmProvider] = []
    local_base = os.environ.get("LOCAL_LLM_BASE_URL") or settings.local_llm_base_url
    local_model = os.environ.get("LOCAL_LLM_CHAT_MODEL") or settings.local_llm_chat_model
    if local_base and local_model:
        providers.append(LlmProvider("local", local_base, local_model))

    openai_key = os.environ.get("OPENAI_API_KEY") or settings.openai_api_key
    openai_base = (
        os.environ.get("OPENAI_API_BASE") or settings.openai_api_base or DEFAULT_OPENAI_BASE_URL
    )
    openai_model = os.environ.get("OPENAI_MODEL") or settings.openai_model
    if openai_key and openai_model:
        providers.append(LlmProvider("openai", openai_base, openai_model, openai_key))

    if selected == "local":
        return [provider for provider in providers if provider.name == "local"]
    if selected == "openai":
        return [provider for provider in providers if provider.name == "openai"]
    return providers


class FallbackChatClient:
    def __init__(
        self,
        providers: Sequence[LlmProvider] | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.providers = list(providers if providers is not None else configured_llm_providers())
        self.timeout = timeout
        self._clients: dict[str, httpx.Client] = {}
        self.last_provider: str | None = None
        self.last_model: str | None = None

    @classmethod
    def from_env(cls, *, timeout: float = 30.0) -> FallbackChatClient:
        return cls(timeout=timeout)

    @property
    def provider_names(self) -> list[str]:
        return [provider.name for provider in self.providers]

    @property
    def has_providers(self) -> bool:
        return bool(self.providers)

    def _client_for(self, provider: LlmProvider) -> httpx.Client:
        existing = self._clients.get(provider.name)
        if existing is not None:
            return existing
        client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            trust_env=provider.trust_env,
        )
        self._clients[provider.name] = client
        return client

    def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        temperature: float = 0.0,
        max_tokens: int = 200,
        response_format: dict[str, Any] | None = None,
        response_validator: Callable[[str], bool] | None = None,
    ) -> LlmChatResult:
        if not self.providers:
            raise LlmFallbackUnavailable("no LLM providers configured")

        errors: list[str] = []
        for provider in self.providers:
            payload: dict[str, Any] = {
                "model": provider.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format is not None:
                payload["response_format"] = response_format
            headers = {}
            if provider.api_key:
                headers["Authorization"] = f"Bearer {provider.api_key}"
            try:
                response = self._client_for(provider).post(
                    provider.endpoint,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"] or ""
                if response_validator is not None and not response_validator(content):
                    raise ValueError("provider returned invalid content")
                self.last_provider = provider.name
                self.last_model = provider.model
                logger.info(
                    "LLM request served by provider=%s model=%s", provider.name, provider.model
                )
                return LlmChatResult(
                    content=content,
                    provider=provider.name,
                    model=provider.model,
                    raw=data,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider.name}:{type(exc).__name__}")
                logger.warning(
                    "LLM provider failed: provider=%s model=%s error=%s",
                    provider.name,
                    provider.model,
                    type(exc).__name__,
                )
        raise LlmFallbackUnavailable("all LLM providers failed: " + ", ".join(errors))

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    def __enter__(self) -> FallbackChatClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
