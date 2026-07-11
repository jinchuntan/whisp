"""Chatbot (voice-assistant answer) provider system.

Selection is driven ONLY by ``CHATBOT_MODE`` (disabled | mock | ollama |
openai_compatible), independent of ``TRANSCRIPTION_MODE`` and Agora. ``disabled``
builds no provider, so the worker makes zero chatbot network calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persephone_worker.chatbot.base import (
    ChatbotContext,
    ChatbotError,
    ChatbotProvider,
    ChatbotResult,
    ChatbotTimeout,
    ChatbotUnavailable,
    EmptyAnswer,
    build_messages,
    sanitize_answer,
    validate_answer,
)
from persephone_worker.chatbot.providers import (
    MockChatbotProvider,
    OllamaChatbotProvider,
    OpenAICompatibleChatbotProvider,
)

if TYPE_CHECKING:
    from persephone_worker.config import WorkerSettings

__all__ = [
    "ChatbotContext",
    "ChatbotError",
    "ChatbotProvider",
    "ChatbotResult",
    "ChatbotTimeout",
    "ChatbotUnavailable",
    "EmptyAnswer",
    "MockChatbotProvider",
    "OllamaChatbotProvider",
    "OpenAICompatibleChatbotProvider",
    "build_chatbot_provider",
    "build_messages",
    "sanitize_answer",
    "validate_answer",
]


def build_chatbot_provider(settings: WorkerSettings) -> ChatbotProvider | None:
    """Construct the provider selected by ``CHATBOT_MODE``.

    Returns ``None`` for ``disabled`` (the assistant loop then does nothing and
    never touches the network). Construction never contacts a model; HTTP
    providers validate config lazily on ``generate`` so the worker still starts
    even if a base URL/model is missing.
    """
    mode = settings.chatbot_mode
    if mode == "disabled":
        return None
    if mode == "mock":
        return MockChatbotProvider(model=settings.chatbot_model or "mock")

    common = {
        "base_url": settings.chatbot_base_url,
        "model": settings.chatbot_model,
        "api_key": settings.chatbot_api_key,
        "temperature": settings.chatbot_temperature,
        "max_output_tokens": settings.chatbot_max_output_tokens,
        "timeout_seconds": settings.chatbot_timeout_seconds,
    }
    if mode == "ollama":
        return OllamaChatbotProvider(**common)
    if mode == "openai_compatible":
        return OpenAICompatibleChatbotProvider(**common)
    # config validator guarantees we never reach here.
    raise ValueError(f"Unknown CHATBOT_MODE: {mode!r}")
