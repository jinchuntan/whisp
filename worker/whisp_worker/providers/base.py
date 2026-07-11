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
        super().__init__(message, code="timeout", **kw)


class ProviderUnavailable(ProviderError):
    code = "unavailable"

    def __init__(self, message: str = "Provider unavailable", **kw: Any):
        kw.setdefault("safe_message", "Transcription provider unavailable")
        super().__init__(message, code="unavailable", **kw)


class EmptyTranscript(ProviderError):
    code = "empty"

    def __init__(self, message: str = "No usable transcript", **kw: Any):
        kw.setdefault("safe_message", "No speech detected")
        super().__init__(message, code="empty", **kw)


@runtime_checkable
class TranscriptionProvider(Protocol):
    name: str

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult: ...
