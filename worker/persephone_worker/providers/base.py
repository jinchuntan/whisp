"""Provider interface, result type, attempt record, and error taxonomy.

Nothing here imports faster-whisper or Agora; concrete providers keep heavy
imports inside their own methods so the router and tests stay lightweight.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass
class TranscriptionResult:
    """Successful transcription output. Contains NO credentials."""

    transcript: str
    provider: str
    processing_ms: int
    language: str | None = None
    confidence: float | None = None
    # Safe-to-store provider metadata (task ids, model name, etc.) — never tokens.
    raw_metadata: dict[str, Any] = field(default_factory=dict)


# --- Attempt outcome statuses (mirror transcription_attempts.status) ---------
ATTEMPT_SUCCESS = "success"
ATTEMPT_EMPTY = "empty"
ATTEMPT_ERROR = "error"
ATTEMPT_TIMEOUT = "timeout"
ATTEMPT_SKIPPED = "skipped"

# --- Agora safe error codes (only these strings are stored/logged; no secrets) -
AGORA_NOT_CONFIGURED = "agora_not_configured"
AGORA_LIVE_DISABLED = "agora_live_disabled"
AGORA_SDK_UNAVAILABLE = "agora_sdk_unavailable"
AGORA_TOKEN_ERROR = "agora_token_error"
AGORA_UNSUPPORTED_AUDIO = "agora_unsupported_audio"
AGORA_RTC_JOIN_FAILED = "agora_rtc_join_failed"
AGORA_RTC_JOIN_TIMEOUT = "agora_rtc_join_timeout"
AGORA_STT_START_FAILED = "agora_stt_start_failed"
AGORA_PUBLISH_FAILED = "agora_publish_failed"
AGORA_CAPTION_TIMEOUT = "agora_caption_timeout"
AGORA_EMPTY_TRANSCRIPT = "agora_empty_transcript"
AGORA_CLEANUP_FAILED = "agora_cleanup_failed"
AGORA_CREDIT_LIMIT_REACHED = "agora_credit_limit_reached"


@dataclass
class AttemptRecord:
    provider: str
    attempt_order: int
    status: str
    latency_ms: int
    started_at: float
    finished_at: float
    safe_error_code: str | None = None
    safe_error_message: str | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """Base provider failure. Carries a SAFE (logging/DB) message and code."""

    code = "provider_error"

    def __init__(self, message: str, *, code: str | None = None, safe_message: str | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.safe_message = safe_message or "Transcription failed"


class ProviderTimeout(ProviderError):
    code = "timeout"

    def __init__(self, message: str = "Provider timed out", **kw: Any):
        kw.setdefault("safe_message", "Transcription timed out")
        kw.setdefault("code", "timeout")  # callers may override with a specific code
        super().__init__(message, **kw)


class ProviderUnavailable(ProviderError):
    code = "unavailable"

    def __init__(self, message: str = "Provider unavailable", **kw: Any):
        kw.setdefault("safe_message", "Transcription provider unavailable")
        kw.setdefault("code", "unavailable")
        super().__init__(message, **kw)


class EmptyTranscript(ProviderError):
    code = "empty"

    def __init__(self, message: str = "No usable transcript", **kw: Any):
        kw.setdefault("safe_message", "No speech detected")
        kw.setdefault("code", "empty")
        super().__init__(message, **kw)


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult: ...
