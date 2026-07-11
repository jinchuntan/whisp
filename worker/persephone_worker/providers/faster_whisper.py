"""Local Faster-Whisper provider.

The model is loaded once per process (lazily, on first use or via ``load()``),
never per question. ``faster_whisper`` is imported inside methods so the module
imports cleanly in environments/tests without the package installed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from persephone_worker.providers.base import ProviderError, TranscriptionResult

log = logging.getLogger("persephone.provider.faster_whisper")


class FasterWhisperProvider:
    name = "faster_whisper"

    def __init__(
        self,
        *,
        model_name: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: str | None = "en",
        beam_size: int = 5,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.beam_size = beam_size
        self._model: Any = None

    def load(self) -> None:
        """Load the model now and report status (called at worker startup)."""
        self._ensure_model()

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ProviderError(
                "faster-whisper is not installed",
                code="unavailable",
                safe_message="Local transcription unavailable",
            ) from exc

        log.info(
            "loading faster-whisper model=%s device=%s compute=%s ...",
            self.model_name,
            self.device,
            self.compute_type,
        )
        t0 = time.monotonic()
        self._model = WhisperModel(
            self.model_name, device=self.device, compute_type=self.compute_type
        )
        log.info("faster-whisper model loaded in %.1fs", time.monotonic() - t0)
        return self._model

    def _run(self, model: Any, audio_path: Path) -> tuple[str, dict[str, Any]]:
        # faster-whisper decodes standard PCM WAV via bundled libs — no separate
        # ffmpeg install is needed for our 16 kHz mono PCM16 input.
        segments, info = model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=self.beam_size,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        meta = {
            "model": self.model_name,
            "beam_size": self.beam_size,
            "language": getattr(info, "language", None),
            "language_probability": round(
                float(getattr(info, "language_probability", 0.0) or 0.0), 4
            ),
            "duration": round(float(getattr(info, "duration", 0.0) or 0.0), 3),
        }
        return text, meta

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult:
        model = await asyncio.to_thread(self._ensure_model)
        t0 = time.monotonic()
        text, meta = await asyncio.to_thread(self._run, model, audio_path)
        processing_ms = int((time.monotonic() - t0) * 1000)
        return TranscriptionResult(
            transcript=text,
            provider=self.name,
            processing_ms=processing_ms,
            language=meta.get("language"),
            confidence=meta.get("language_probability"),
            raw_metadata=meta,
        )
