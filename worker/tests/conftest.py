"""Worker test fixtures and fakes. No heavy ML packages, no network, no Agora."""

from __future__ import annotations

import asyncio
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from whisp_worker.providers.base import (
    EmptyTranscript,
    ProviderError,
    TranscriptionResult,
)


class FakeProvider:
    """Configurable fake transcription provider for router tests."""

    def __init__(
        self,
        name: str,
        *,
        transcript: str = "hello world",
        raises: Exception | None = None,
        empty: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.name = name
        self._transcript = transcript
        self._raises = raises
        self._empty = empty
        self._delay = delay
        self.calls = 0

    async def transcribe(self, audio_path: Path, question_id: str) -> TranscriptionResult:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        if self._empty:
            return TranscriptionResult(transcript="", provider=self.name, processing_ms=1)
        return TranscriptionResult(
            transcript=self._transcript, provider=self.name, processing_ms=5, language="en"
        )


class FakeEmbedder:
    """Deterministic embedder: maps configured phrases to fixed vectors."""

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = table or {}

    def embed(self, text: str) -> list[float]:
        if text in self.table:
            return self.table[text]
        # Fallback: crude bag-of-chars vector so unknown text is deterministic.
        vec = [0.0] * 5
        for ch in text.lower():
            vec[ord(ch) % 5] += 1.0
        return vec


class FakeResult:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeSupabaseClient:
    """Very small stand-in for the supabase client used by JobQueue tests.

    Records rpc/table operations so tests can assert on them.
    """

    def __init__(self) -> None:
        self.rpc_calls: list[tuple[str, dict]] = []
        self.rpc_returns: dict[str, Any] = {}
        self.updates: list[tuple[str, dict]] = []
        self.inserts: list[tuple[str, dict]] = []

    # rpc(name, params).execute()
    def rpc(self, name: str, params: dict) -> Any:
        self.rpc_calls.append((name, params))
        client = self

        class _Exec:
            def execute(self_inner) -> FakeResult:
                return FakeResult(client.rpc_returns.get(name))

        return _Exec()

    def table(self, name: str) -> Any:
        return _FakeTable(self, name)


class _FakeTable:
    def __init__(self, client: FakeSupabaseClient, name: str) -> None:
        self._c = client
        self._name = name
        self._payload: dict | None = None
        self._op: str | None = None

    def update(self, payload: dict) -> _FakeTable:
        self._op = "update"
        self._payload = payload
        return self

    def insert(self, payload: dict) -> _FakeTable:
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload: dict, **_: Any) -> _FakeTable:
        self._op = "upsert"
        self._payload = payload
        return self

    def eq(self, *_: Any) -> _FakeTable:
        return self

    def execute(self) -> FakeResult:
        if self._op == "update":
            self._c.updates.append((self._name, self._payload or {}))
        elif self._op in ("insert", "upsert"):
            self._c.inserts.append((self._name, self._payload or {}))
        return FakeResult(self._payload)


def write_wav(path: Path, seconds: float = 0.2, rate: int = 16000) -> Path:
    n = int(seconds * rate)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return path


@pytest.fixture
def wav_file(tmp_path: Path) -> Path:
    return write_wav(tmp_path / "sample.wav")


@pytest.fixture
def dummy_audio(tmp_path: Path) -> Path:
    return tmp_path / "dummy.wav"


# Re-export helpers for tests
__all__ = [
    "FakeProvider",
    "FakeEmbedder",
    "FakeSupabaseClient",
    "EmptyTranscript",
    "ProviderError",
    "write_wav",
]
