"""Domain enums and constants shared across the web/API layer.

These mirror the CHECK constraints in supabase/migrations/001_initial_schema.sql.
Keep the two in sync.
"""

from __future__ import annotations

from enum import Enum

API_PREFIX = "/api/v1"


class QuestionStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    TRANSCRIBING = "transcribing"
    DONE = "done"
    EMPTY = "empty"
    ERROR = "error"


class RoundStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ClusterStatus(str, Enum):
    OPEN = "open"
    ANSWERED = "answered"


class TranscriptionMode(str, Enum):
    """Authoritative provider-selection modes (see providers/router.py)."""

    AGORA_FIRST = "agora_first"
    FASTER_WHISPER_FIRST = "faster_whisper_first"
    AGORA_ONLY = "agora_only"
    FASTER_WHISPER_ONLY = "faster_whisper_only"


DEFAULT_TRANSCRIPTION_MODE = TranscriptionMode.FASTER_WHISPER_ONLY

# Modes in which the worker may contact Agora and therefore spend credit.
AGORA_MODES = {TranscriptionMode.AGORA_FIRST, TranscriptionMode.AGORA_ONLY}

# Statuses the badge should treat as "still working, keep polling".
IN_PROGRESS_STATUSES = {QuestionStatus.QUEUED, QuestionStatus.CLAIMED, QuestionStatus.TRANSCRIBING}
