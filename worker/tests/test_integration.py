"""OPTIONAL manual integration tests — SKIPPED by default.

These are the only tests that may touch real, heavy dependencies. They never run
in the normal suite (no Agora credit, no model download in CI). Enable them
deliberately:

    # Real Faster-Whisper on a spoken WAV (downloads the model on first run):
    WHISP_RUN_INTEGRATION=1 WHISP_SAMPLE_WAV=/path/to/speech.wav \
        ../.venv/bin/python -m pytest tests/test_integration.py -q

    # Real Supabase round-trip (needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY):
    WHISP_RUN_INTEGRATION=1 WHISP_TEST_SUPABASE=1 ... pytest tests/test_integration.py -q
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

RUN = os.environ.get("WHISP_RUN_INTEGRATION") == "1"

pytestmark = pytest.mark.skipif(
    not RUN, reason="set WHISP_RUN_INTEGRATION=1 to run manual integration tests"
)


def test_faster_whisper_transcribes_real_wav():
    """Transcribe a real spoken WAV with the real model (no mocks)."""
    sample = os.environ.get("WHISP_SAMPLE_WAV")
    if not sample or not Path(sample).is_file():
        pytest.skip("set WHISP_SAMPLE_WAV=/path/to/speech.wav (16 kHz mono PCM16)")

    import asyncio

    from whisp_worker.providers.faster_whisper import FasterWhisperProvider

    provider = FasterWhisperProvider(model_name="base", device="cpu", compute_type="int8")
    result = asyncio.run(provider.transcribe(Path(sample), "integration"))
    assert result.provider == "faster_whisper"
    assert result.transcript.strip(), "expected non-empty transcript for a speech sample"
    print(f"\nTranscript: {result.transcript!r}  ({result.processing_ms} ms)")


def test_supabase_claim_roundtrip():
    """Insert a queued question and claim it via the real RPC."""
    if os.environ.get("WHISP_TEST_SUPABASE") != "1":
        pytest.skip("set WHISP_TEST_SUPABASE=1 (uses real Supabase credentials)")

    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    from whisp_worker.queue import JobQueue

    q = JobQueue(client, os.environ.get("SUPABASE_AUDIO_BUCKET", "whisp-audio"), "itest-worker")
    inserted = client.table("questions").insert({"badge_id": "itest", "status": "queued"}).execute()
    qid = inserted.data[0]["id"]
    try:
        claimed = q.claim(60)
        assert claimed is not None
        assert claimed["status"] == "claimed"
    finally:
        client.table("questions").delete().eq("id", qid).execute()
