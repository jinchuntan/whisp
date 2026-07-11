"""Mode-driven provider router.

The ONLY thing that decides provider order and fallback is ``TRANSCRIPTION_MODE``.
Providers are constructed lazily from a factory registry, so a mode that does not
include a provider never constructs it (e.g. ``faster_whisper_only`` never builds,
initializes, or calls Agora — proven in worker/tests/test_router.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from whisp_worker.providers.base import (
    ATTEMPT_EMPTY,
    ATTEMPT_ERROR,
    ATTEMPT_SUCCESS,
    ATTEMPT_TIMEOUT,
    AttemptRecord,
    EmptyTranscript,
    ProviderError,
    TranscriptionProvider,
    TranscriptionResult,
)

log = logging.getLogger("whisp.router")

FASTER_WHISPER = "faster_whisper"
AGORA = "agora"

# Ordered provider list per mode. Length 1 => no fallback (the "*_only" modes).
MODE_ORDER: dict[str, list[str]] = {
    "agora_first": [AGORA, FASTER_WHISPER],
    "faster_whisper_first": [FASTER_WHISPER, AGORA],
    "agora_only": [AGORA],
    "faster_whisper_only": [FASTER_WHISPER],
}

DEFAULT_TIMEOUTS = {FASTER_WHISPER: 120.0, AGORA: 60.0}

ProviderFactory = Callable[[], TranscriptionProvider]


@dataclass
class RouterOutcome:
    status: str  # "done" | "empty" | "error"
    result: TranscriptionResult | None = None
    provider_used: str | None = None
    fallback_used: bool = False
    attempts: list[AttemptRecord] = field(default_factory=list)
    error_code: str | None = None
    safe_error_message: str | None = None


class ProviderRouter:
    def __init__(
        self,
        mode: str,
        factories: dict[str, ProviderFactory],
        *,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        if mode not in MODE_ORDER:
            raise ValueError(f"Unknown TRANSCRIPTION_MODE: {mode!r}")
        self.mode = mode
        self.factories = factories
        self.timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}

    @property
    def order(self) -> list[str]:
        return MODE_ORDER[self.mode]

    async def transcribe(self, audio_path: Path, question_id: str) -> RouterOutcome:
        attempts: list[AttemptRecord] = []
        last_code: str | None = None
        last_msg: str | None = None

        for index, name in enumerate(self.order):
            factory = self.factories.get(name)
            if factory is None:
                raise RuntimeError(f"No provider factory registered for {name!r}")

            started = time.monotonic()
            try:
                provider = factory()  # lazy: Agora is only constructed if selected
                timeout = self.timeouts.get(name, 120.0)
                result = await asyncio.wait_for(
                    provider.transcribe(audio_path, question_id), timeout=timeout
                )
                result.provider = name
                if not result.transcript or not result.transcript.strip():
                    attempts.append(
                        self._record(
                            name,
                            index,
                            ATTEMPT_EMPTY,
                            started,
                            meta=result.raw_metadata,
                            code="empty",
                            msg="No speech detected",
                        )
                    )
                    last_code, last_msg = "empty", "No speech detected"
                    log.info("q=%s provider=%s attempt=%d -> empty", question_id, name, index)
                    continue

                attempts.append(
                    self._record(name, index, ATTEMPT_SUCCESS, started, meta=result.raw_metadata)
                )
                fallback_used = index > 0
                log.info(
                    "q=%s provider=%s attempt=%d -> success (%dms, fallback=%s)",
                    question_id,
                    name,
                    index,
                    result.processing_ms,
                    fallback_used,
                )
                return RouterOutcome(
                    status="done",
                    result=result,
                    provider_used=name,
                    fallback_used=fallback_used,
                    attempts=attempts,
                )

            except asyncio.TimeoutError:
                attempts.append(
                    self._record(
                        name,
                        index,
                        ATTEMPT_TIMEOUT,
                        started,
                        code="timeout",
                        msg="Transcription timed out",
                    )
                )
                last_code, last_msg = "timeout", "Transcription timed out"
                log.warning("q=%s provider=%s attempt=%d -> timeout", question_id, name, index)
            except EmptyTranscript as exc:
                attempts.append(
                    self._record(
                        name, index, ATTEMPT_EMPTY, started, code=exc.code, msg=exc.safe_message
                    )
                )
                last_code, last_msg = exc.code, exc.safe_message
                log.info("q=%s provider=%s attempt=%d -> empty", question_id, name, index)
            except ProviderError as exc:
                attempts.append(
                    self._record(
                        name, index, ATTEMPT_ERROR, started, code=exc.code, msg=exc.safe_message
                    )
                )
                last_code, last_msg = exc.code, exc.safe_message
                log.warning(
                    "q=%s provider=%s attempt=%d -> error(%s)", question_id, name, index, exc.code
                )
            except Exception:
                # Never leak internal exception detail into stored/safe fields.
                attempts.append(
                    self._record(
                        name,
                        index,
                        ATTEMPT_ERROR,
                        started,
                        code="internal_error",
                        msg="Transcription failed",
                    )
                )
                last_code, last_msg = "internal_error", "Transcription failed"
                log.exception(
                    "q=%s provider=%s attempt=%d -> unexpected error", question_id, name, index
                )

        # All providers in the order exhausted with no success.
        all_empty = bool(attempts) and all(a.status == ATTEMPT_EMPTY for a in attempts)
        if all_empty:
            return RouterOutcome(
                status="empty",
                attempts=attempts,
                error_code="empty",
                safe_error_message="No speech detected",
            )
        return RouterOutcome(
            status="error",
            attempts=attempts,
            error_code=last_code or "error",
            safe_error_message=last_msg or "Transcription unavailable",
        )

    @staticmethod
    def _record(
        name: str,
        order: int,
        status: str,
        started: float,
        *,
        code: str | None = None,
        msg: str | None = None,
        meta: dict | None = None,
    ) -> AttemptRecord:
        finished = time.monotonic()
        return AttemptRecord(
            provider=name,
            attempt_order=order,
            status=status,
            latency_ms=int((finished - started) * 1000),
            started_at=started,
            finished_at=finished,
            safe_error_code=code,
            safe_error_message=msg,
            provider_metadata=_safe_meta(meta),
        )


def _safe_meta(meta: dict | None) -> dict:
    """Keep only JSON-serialisable, non-sensitive metadata."""
    if not meta:
        return {}
    out: dict = {}
    for k, v in meta.items():
        if any(s in k.lower() for s in ("token", "secret", "key", "cert", "password", "auth")):
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out
