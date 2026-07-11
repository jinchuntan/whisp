"""Wire-contract tests: WAV validation and the exact poll response shapes the
badge firmware depends on."""

from __future__ import annotations

import struct

import pytest

from tests.conftest import make_wav
from whisp_api.wav import WavValidationError, parse_wav


# --------------------------- WAV validation -------------------------------
def test_parse_valid_wav():
    info = parse_wav(make_wav(0.1))
    assert info.audio_format == 1
    assert info.channels == 1
    assert info.sample_rate == 16000
    assert info.bits_per_sample == 16


def test_parse_rejects_too_small():
    with pytest.raises(WavValidationError):
        parse_wav(b"RIFF")


def test_parse_rejects_missing_riff():
    with pytest.raises(WavValidationError):
        parse_wav(b"XXXX" + b"\x00" * 60)


def test_parse_rejects_stereo():
    header = b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, 2, 16000, 64000, 4, 16)  # 2 channels
    header += b"data" + struct.pack("<I", 0)
    with pytest.raises(WavValidationError):
        parse_wav(header)


def test_parse_rejects_non_pcm():
    header = b"RIFF" + struct.pack("<I", 36) + b"WAVE" + b"fmt "
    header += struct.pack("<IHHIIHH", 16, 3, 1, 16000, 64000, 4, 16)  # format 3 (float)
    header += b"data" + struct.pack("<I", 0)
    with pytest.raises(WavValidationError):
        parse_wav(header)


# --------------------------- poll response shapes -------------------------
def _put_question(fake_db, **overrides):
    q = {
        "id": "q-1",
        "status": "queued",
        "transcript": None,
        "provider_used": None,
        "fallback_used": False,
        "cluster_id": None,
        "safe_error_message": None,
    }
    q.update(overrides)
    fake_db.questions["q-1"] = q
    return q


BADGE = {"X-Whisp-Key": "test-badge-key"}


def test_poll_queued(client, fake_db):
    _put_question(fake_db, status="queued")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body == {
        "question_id": "q-1",
        "status": "queued",
        "transcript": None,
        "provider": None,
        "fallback_used": None,
        "similar_count": None,
        "cluster_id": None,
        "message": None,
    }


def test_poll_claimed_maps_to_queued(client, fake_db):
    _put_question(fake_db, status="claimed")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["status"] == "queued"


def test_poll_transcribing(client, fake_db):
    _put_question(fake_db, status="transcribing")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["status"] == "transcribing"


def test_poll_done_with_cluster(client, fake_db):
    cl = fake_db.seed_cluster("r-1", "How can AI improve participation?", count=3)
    _put_question(
        fake_db,
        status="done",
        transcript="How can AI improve participation?",
        provider_used="faster_whisper",
        fallback_used=False,
        cluster_id=cl["id"],
    )
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["status"] == "done"
    assert body["transcript"] == "How can AI improve participation?"
    assert body["provider"] == "faster_whisper"
    assert body["fallback_used"] is False
    assert body["similar_count"] == 3
    assert body["cluster_id"] == cl["id"]


def test_poll_done_without_cluster_similar_count_1(client, fake_db):
    _put_question(fake_db, status="done", transcript="hi", provider_used="faster_whisper")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["similar_count"] == 1


def test_poll_done_fallback_flag(client, fake_db):
    _put_question(
        fake_db,
        status="done",
        transcript="x",
        provider_used="faster_whisper",
        fallback_used=True,
    )
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["fallback_used"] is True


def test_poll_empty(client, fake_db):
    _put_question(fake_db, status="empty")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["status"] == "empty"
    assert body["message"] == "No speech detected"


def test_poll_error_is_safe(client, fake_db):
    _put_question(fake_db, status="error", safe_error_message="Transcription unavailable")
    body = client.get("/api/v1/questions/q-1", headers=BADGE).json()
    assert body["status"] == "error"
    assert body["message"] == "Transcription unavailable"
    assert "transcript" not in body or body["transcript"] is None


def test_poll_unknown_404(client):
    assert client.get("/api/v1/questions/does-not-exist", headers=BADGE).status_code == 404


def test_upload_response_contract(client, fake_db):
    fake_db.seed_event()
    r = client.post(
        "/api/v1/questions",
        content=make_wav(),
        headers={"X-Whisp-Key": "test-badge-key", "X-Badge-Id": "badge-001"},
    )
    assert r.status_code == 202
    body = r.json()
    assert set(body.keys()) == {"ok", "question_id", "status", "poll_url"}
    assert body["poll_url"].startswith("/api/v1/questions/")
