"""Concrete chatbot providers: mock, Ollama, and OpenAI-compatible.

The two HTTP providers share a small base that takes an injectable async
transport, so tests exercise the exact request/response shaping without any
network (and therefore never spend LLM credit or contact Ollama). ``httpx`` is
imported lazily inside the default transport only.

Nothing here logs API keys, Authorization headers, or raw provider bodies.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from persephone_worker.chatbot.base import (
    CHATBOT_BAD_RESPONSE,
    CHATBOT_HTTP_ERROR,
    CHATBOT_NOT_CONFIGURED,
    ChatbotContext,
    ChatbotError,
    ChatbotResult,
    ChatbotTimeout,
    ChatbotUnavailable,
    build_messages,
    validate_answer,
)

log = logging.getLogger("persephone.chatbot")

# transport(url, headers, json_body) -> parsed JSON dict. Injected in tests.
Transport = Callable[[str, dict[str, str], dict[str, Any]], Awaitable[dict[str, Any]]]

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"


class _TransportError(Exception):
    """Internal: transport failed in a way that maps to a safe HTTP error."""


class _TransportTimeout(Exception):
    """Internal: transport timed out."""


# ---------------------------------------------------------------------------
# Mock — deterministic, offline. For tests and credential-free demos.
# ---------------------------------------------------------------------------
class MockChatbotProvider:
    name = "mock"

    def __init__(self, model: str = "mock", *, fail: bool = False, empty: bool = False) -> None:
        self.model = model
        self._fail = fail
        self._empty = empty

    async def generate(self, ctx: ChatbotContext) -> ChatbotResult:
        if self._fail:
            raise ChatbotUnavailable("mock chatbot failure")
        if self._empty:
            return ChatbotResult(text="", provider=self.name, model=self.model, processing_ms=0)
        # Deterministic: no timestamps, no randomness. A short, playback-safe reply
        # that references the topic when one is set.
        topic = (ctx.round_prompt or "").strip()
        if topic:
            body = (
                f"That's a good question about {topic.rstrip('?.')}. "
                "Here's a short, spoken answer generated in mock mode for testing. "
                "It stays brief so it plays back cleanly through the speaker."
            )
        else:
            body = (
                "Thanks for the question. This is a short mock answer used for "
                "testing the voice assistant. It stays brief so it plays back "
                "cleanly through the speaker."
            )
        return ChatbotResult(text=body, provider=self.name, model=self.model, processing_ms=0)


# ---------------------------------------------------------------------------
# HTTP base
# ---------------------------------------------------------------------------
class _HttpChatbotProvider:
    """Shared HTTP plumbing for the network-backed providers."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.3,
        max_output_tokens: int = 180,
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self._transport = transport

    # -- subclass hooks --
    def _endpoint(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, data: dict[str, Any]) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def _require_config(self) -> None:
        if not self.base_url or not self.model:
            raise ChatbotUnavailable(
                "chatbot base_url/model not configured",
                code=CHATBOT_NOT_CONFIGURED,
                safe_message="Assistant not configured",
            )

    async def generate(self, ctx: ChatbotContext) -> ChatbotResult:
        self._require_config()
        messages = build_messages(ctx)
        body = self._body(messages)
        t0 = time.monotonic()
        try:
            data = await self._send(self._endpoint(), self._headers(), body)
        except _TransportTimeout as exc:
            raise ChatbotTimeout("chatbot request timed out") from exc
        except _TransportError as exc:
            # Never include the provider response body (may echo the prompt/keys).
            raise ChatbotError(
                "chatbot HTTP error",
                code=CHATBOT_HTTP_ERROR,
                safe_message="Assistant request failed",
            ) from exc
        processing_ms = int((time.monotonic() - t0) * 1000)
        try:
            text = self._parse(data)
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise ChatbotError(
                "unexpected chatbot response shape",
                code=CHATBOT_BAD_RESPONSE,
                safe_message="Assistant returned an unusable response",
            ) from exc
        # Validate (sanitise + non-empty) here so an empty completion is a typed error.
        text = validate_answer(text)
        return ChatbotResult(
            text=text, provider=self.name, model=self.model, processing_ms=processing_ms
        )

    async def _send(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        if self._transport is not None:
            return await self._transport(url, headers, body)
        return await self._httpx_send(url, headers, body)

    async def _httpx_send(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise _TransportTimeout(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise _TransportError(str(exc)) from exc
        if resp.status_code >= 400:
            # Status only — never the body (it can echo the prompt or a key).
            raise _TransportError(f"status {resp.status_code}")
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - any decode failure is a bad response
            raise _TransportError("invalid JSON") from exc


# ---------------------------------------------------------------------------
# Ollama (native /api/chat — stable, no OpenAI-compat layer required)
# ---------------------------------------------------------------------------
class OllamaChatbotProvider(_HttpChatbotProvider):
    """Local Ollama via its native chat endpoint: POST {base}/api/chat.

    Verified request/response shape (Ollama /api/chat, non-streaming):
      body     -> {"model", "messages", "stream": false, "options": {...}}
      response -> {"message": {"role": "assistant", "content": "..."}, "done": true}
    Ollama also exposes an OpenAI-compatible endpoint at {base}/v1/chat/completions
    (use CHATBOT_MODE=openai_compatible with CHATBOT_BASE_URL=.../v1 for that).
    """

    name = "ollama"

    def __init__(self, **kw: Any) -> None:
        if not (kw.get("base_url") or "").strip():
            kw["base_url"] = DEFAULT_OLLAMA_BASE_URL
        super().__init__(**kw)

    def _endpoint(self) -> str:
        return f"{self.base_url}/api/chat"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Ollama ignores auth by default; only send it if the operator set one
        # (e.g. a reverse proxy in front of Ollama).
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_output_tokens,
            },
        }

    def _parse(self, data: dict[str, Any]) -> str:
        return str(data["message"]["content"])


# ---------------------------------------------------------------------------
# OpenAI-compatible (/chat/completions on any compatible base URL)
# ---------------------------------------------------------------------------
class OpenAICompatibleChatbotProvider(_HttpChatbotProvider):
    """Any OpenAI-compatible chat-completions server.

    POST {base}/chat/completions with the standard schema. Set CHATBOT_BASE_URL
    to the API base that already ends in the version segment, e.g.
    ``https://api.openai.com/v1`` or ``http://localhost:11434/v1`` (Ollama's
    OpenAI-compatible endpoint). CHATBOT_API_KEY is sent as a Bearer token.
    """

    name = "openai_compatible"

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
            "stream": False,
        }

    def _parse(self, data: dict[str, Any]) -> str:
        return str(data["choices"][0]["message"]["content"])
