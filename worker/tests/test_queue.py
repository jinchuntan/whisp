"""JobQueue: maps the claim RPC and status writes onto the client (faked)."""

from __future__ import annotations

from tests.conftest import FakeSupabaseClient
from whisp_worker.providers.base import AttemptRecord, TranscriptionResult
from whisp_worker.queue import JobQueue


def make_queue():
    client = FakeSupabaseClient()
    return JobQueue(client, "whisp-audio", "worker-test"), client


def test_claim_calls_rpc_with_params_and_returns_row():
    q, client = make_queue()
    client.rpc_returns["claim_next_question"] = [{"id": "q1", "status": "claimed"}]
    row = q.claim(120)
    assert row == {"id": "q1", "status": "claimed"}
    name, params = client.rpc_calls[0]
    assert name == "claim_next_question"
    assert params == {"p_worker_id": "worker-test", "p_lease_seconds": 120}


def test_claim_returns_none_when_empty():
    q, client = make_queue()
    client.rpc_returns["claim_next_question"] = []
    assert q.claim(120) is None


def test_complete_writes_done():
    q, client = make_queue()
    result = TranscriptionResult(
        transcript="hello", provider="faster_whisper", processing_ms=42, language="en"
    )
    q.complete("q1", result, fallback_used=True)
    table, payload = client.updates[-1]
    assert table == "questions"
    assert payload["status"] == "done"
    assert payload["transcript"] == "hello"
    assert payload["provider_used"] == "faster_whisper"
    assert payload["fallback_used"] is True
    assert payload["processing_ms"] == 42


def test_set_empty_and_error():
    q, client = make_queue()
    q.set_empty("q1")
    assert client.updates[-1][1]["status"] == "empty"
    q.set_error("q1", "timeout", "Transcription timed out")
    assert client.updates[-1][1]["status"] == "error"
    assert client.updates[-1][1]["error_code"] == "timeout"


def test_record_attempt_inserts():
    q, client = make_queue()
    attempt = AttemptRecord(
        provider="agora",
        attempt_order=0,
        status="error",
        latency_ms=15,
        started_at=0.0,
        finished_at=0.015,
        safe_error_code="unavailable",
        safe_error_message="Agora unavailable",
        provider_metadata={"channel": "x"},
    )
    q.record_attempt("q1", attempt)
    table, payload = client.inserts[-1]
    assert table == "transcription_attempts"
    assert payload["provider"] == "agora"
    assert payload["question_id"] == "q1"
    assert payload["status"] == "error"


def test_heartbeat_upserts():
    q, client = make_queue()
    q.heartbeat("0.1.0", "faster_whisper_only", "idle")
    table, payload = client.inserts[-1]
    assert table == "worker_heartbeats"
    assert payload["worker_id"] == "worker-test"
    assert payload["transcription_mode"] == "faster_whisper_only"


def test_add_question_to_cluster_returns_count():
    q, client = make_queue()
    client.rpc_returns["add_question_to_cluster"] = 3
    count = q.add_question_to_cluster("c1", "q1", 0.9)
    assert count == 3
    name, params = client.rpc_calls[-1]
    assert name == "add_question_to_cluster"
    assert params["p_cluster_id"] == "c1"
