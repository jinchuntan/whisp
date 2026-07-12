"""Synchronous OpenAI-compatible tool-calling client for the clustering agent.

This mirrors ``chatbot/providers.py`` (injectable transport, lazy ``httpx``, safe
error taxonomy that never logs keys/headers/raw bodies) but is **synchronous** and
speaks the chat-completions *tool-calling* protocol: it POSTs ``messages`` plus
``tools`` + ``tool_choice`` and parses ``choices[0].message.tool_calls``.

Sync on purpose: the worker's clustering seam (``Worker._cluster``) is synchronous
and is called from both the async transcription loop and the sync recluster loop, so
the agent must not introduce async plumbing.

Nothing here logs API keys, Authorization headers, or raw provider bodies. Tests
inject a transport (or use ``MockToolClient``) so no network call is ever made.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("persephone.agent")

# --- Safe error codes (only these strings are stored/logged; never secrets) ----
AGENT_LLM_NOT_CONFIGURED = "agent_llm_not_configured"
AGENT_LLM_TIMEOUT = "agent_llm_timeout"
AGENT_LLM_HTTP_ERROR = "agent_llm_http_error"
AGENT_LLM_BAD_RESPONSE = "agent_llm_bad_response"
AGENT_LLM_UNAVAILABLE = "agent_llm_unavailable"

# transport(url, headers, json_body) -> parsed JSON dict. Synchronous. Injected in
# tests so the exact request/response shaping runs offline.
Transport = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


# --- Error taxonomy ---------------------------------------------------------
class AgentLLMError(Exception):
    """Base tool-client failure. Carries a SAFE (logging) message and code."""

    code = "agent_llm_error"

    def __init__(self, message: str, *, code: str | None = None, safe_message: str | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.safe_message = safe_message or "Agent model call failed"


class AgentLLMTimeout(AgentLLMError):
    code = AGENT_LLM_TIMEOUT

    def __init__(self, message: str = "Agent model timed out", **kw: Any):
        kw.setdefault("safe_message", "Agent model timed out")
        kw.setdefault("code", AGENT_LLM_TIMEOUT)
        super().__init__(message, **kw)


class AgentLLMUnavailable(AgentLLMError):
    code = AGENT_LLM_UNAVAILABLE

    def __init__(self, message: str = "Agent model unavailable", **kw: Any):
        kw.setdefault("safe_message", "Agent model unavailable")
        kw.setdefault("code", AGENT_LLM_UNAVAILABLE)
        super().__init__(message, **kw)


class _TransportError(Exception):
    """Internal: transport failed in a way that maps to a safe HTTP error."""


class _TransportTimeout(Exception):
    """Internal: transport timed out."""


# --- Wire types -------------------------------------------------------------
@dataclass
class ToolCall:
    """One tool call requested by the model (arguments already JSON-parsed)."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """A single assistant turn: any tool calls it made, plus optional text."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    content: str | None = None
    finish_reason: str | None = None


@runtime_checkable
class ToolClient(Protocol):
    name: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Mock — deterministic, offline. For tests and credential-free demos.
# ---------------------------------------------------------------------------
class MockToolClient:
    """Deterministic tool client driven by a scripted sequence of responses.

    Each call to :meth:`chat` returns (and consumes) the next scripted
    ``LLMResponse``. When the script is exhausted it returns ``fallback`` (a plain
    text turn with no tool call). Configure ``raises`` to simulate a transport
    failure/timeout. It makes ZERO network calls and never imports ``httpx``.
    """

    name = "mock"

    def __init__(
        self,
        script: list[LLMResponse] | None = None,
        *,
        raises: Exception | None = None,
        fallback: LLMResponse | None = None,
    ) -> None:
        self._script = list(script or [])
        self._raises = raises
        self._fallback = fallback or LLMResponse(content="", finish_reason="stop")
        self.calls = 0
        self.seen: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        self.calls += 1
        self.seen.append((messages, tools, tool_choice))
        if self._raises is not None:
            raise self._raises
        if self._script:
            return self._script.pop(0)
        return self._fallback


# ---------------------------------------------------------------------------
# OpenAI-compatible (/chat/completions with tools + tool_choice)
# ---------------------------------------------------------------------------
class OpenAICompatibleToolClient:
    """Any OpenAI-compatible chat-completions server that supports tool calls.

    POST ``{base}/chat/completions`` with ``{model, messages, tools, tool_choice,
    temperature, stream:false}`` and parse ``choices[0].message.tool_calls``. Set
    ``base_url`` to the API base that already ends in the version segment (e.g.
    ``https://api.openai.com/v1``). ``api_key`` is sent as a Bearer token.
    """

    name = "openai_compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.0,
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.model = (model or "").strip()
        self._api_key = api_key or ""
        self.temperature = temperature
        self.timeout = timeout_seconds
        self._transport = transport

    def _endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _require_config(self) -> None:
        if not self.base_url or not self.model:
            raise AgentLLMUnavailable(
                "agent base_url/model not configured",
                code=AGENT_LLM_NOT_CONFIGURED,
                safe_message="Agent not configured",
            )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        self._require_config()
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": self.temperature,
            "stream": False,
        }
        try:
            data = self._send(self._endpoint(), self._headers(), body)
        except _TransportTimeout as exc:
            raise AgentLLMTimeout("agent request timed out") from exc
        except _TransportError as exc:
            # Never include the provider response body (it can echo the prompt/keys).
            raise AgentLLMError(
                "agent HTTP error",
                code=AGENT_LLM_HTTP_ERROR,
                safe_message="Agent request failed",
            ) from exc
        return _parse_response(data)

    def _send(self, url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if self._transport is not None:
            return self._transport(url, headers, body)
        return self._httpx_send(url, headers, body)

    def _httpx_send(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        import httpx

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, headers=headers, json=body)
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


def _parse_response(data: dict[str, Any]) -> LLMResponse:
    """Parse an OpenAI-style chat-completions response into an ``LLMResponse``.

    A missing/misshaped envelope or unparseable tool-call arguments is a typed
    ``AgentLLMError`` (bad response) — the caller turns that into a fallback.
    """
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AgentLLMError(
            "unexpected agent response shape",
            code=AGENT_LLM_BAD_RESPONSE,
            safe_message="Agent returned an unusable response",
        ) from exc

    calls: list[ToolCall] = []
    for raw in msg.get("tool_calls") or []:
        try:
            fn = raw["function"]
            name = str(fn["name"])
            args_raw = fn.get("arguments")
        except (KeyError, TypeError) as exc:
            raise AgentLLMError(
                "malformed tool call in agent response",
                code=AGENT_LLM_BAD_RESPONSE,
                safe_message="Agent returned a malformed tool call",
            ) from exc
        arguments = _parse_arguments(args_raw)
        calls.append(ToolCall(id=str(raw.get("id") or ""), name=name, arguments=arguments))

    content = msg.get("content")
    return LLMResponse(
        tool_calls=calls,
        content=content if content is None else str(content),
        finish_reason=choice.get("finish_reason"),
    )


def _parse_arguments(args_raw: Any) -> dict[str, Any]:
    """Tool-call arguments are a JSON string in the wire format; some servers send an
    object. Either is accepted; anything else is a malformed (bad-response) error."""
    if args_raw is None or args_raw == "":
        return {}
    if isinstance(args_raw, dict):
        return args_raw
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise AgentLLMError(
                "unparseable tool-call arguments",
                code=AGENT_LLM_BAD_RESPONSE,
                safe_message="Agent returned malformed tool arguments",
            ) from exc
        if not isinstance(parsed, dict):
            raise AgentLLMError(
                "tool-call arguments were not an object",
                code=AGENT_LLM_BAD_RESPONSE,
                safe_message="Agent returned malformed tool arguments",
            )
        return parsed
    raise AgentLLMError(
        "tool-call arguments had an unexpected type",
        code=AGENT_LLM_BAD_RESPONSE,
        safe_message="Agent returned malformed tool arguments",
    )
