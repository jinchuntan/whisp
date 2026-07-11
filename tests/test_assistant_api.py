"""Voice-assistant API surface: nested state + generate/retry/regenerate.

All against in-memory fakes (no Supabase). Confirms host-only auth + CSRF, that
the badge polling contract is unchanged, and that repeated actions are idempotent.
"""

from __future__ import annotations

import uuid

from tests.conftest import BADGE_KEY, ORIGIN


def _seed_done_question(fake_db, *, round_id=None) -> str:
    qid = str(uuid.uuid4())
    fake_db.questions[qid] = {
        "id": qid,
        "badge_id": "badge-001",
        "status": "done",
        "transcript": "What are the benefits of AI?",
        "event_id": None,
        "round_id": round_id,
        "created_at": "2026-07-12T00:00:00+00:00",
        "answered_at": None,
    }
    return qid


# ---------------------------------------------------------------------------
# Admin state nesting
# ---------------------------------------------------------------------------
def test_admin_state_includes_assistant_response(host_client, fake_db):
    ev = fake_db.seed_event("Conf")
    qid = _seed_done_question(fake_db)
    fake_db.questions[qid]["event_id"] = ev["id"]
    fake_db.seed_assistant_response(qid, status="done", response_text="AI can help a lot.")

    body = host_client.get("/api/v1/admin/state").json()
    q = next(q for q in body["questions"] if q["id"] == qid)
    ar = q["assistant_response"]
    assert ar is not None
    assert ar["status"] == "done"
    assert ar["response_text"] == "AI can help a lot."
    assert ar["provider"] == "mock"


def test_admin_state_question_without_answer_has_null(host_client, fake_db):
    ev = fake_db.seed_event("Conf")
    qid = _seed_done_question(fake_db)
    fake_db.questions[qid]["event_id"] = ev["id"]
    body = host_client.get("/api/v1/admin/state").json()
    q = next(q for q in body["questions"] if q["id"] == qid)
    assert q["assistant_response"] is None


# ---------------------------------------------------------------------------
# Badge polling contract is UNCHANGED (no assistant fields leak to the badge)
# ---------------------------------------------------------------------------
def test_badge_poll_has_no_assistant_fields(client, fake_db):
    qid = _seed_done_question(fake_db)
    fake_db.seed_assistant_response(qid, status="done")
    r = client.get(f"/api/v1/questions/{qid}", headers={"X-Persephone-Key": BADGE_KEY})
    assert r.status_code == 200
    body = r.json()
    assert "assistant_response" not in body
    assert "response_text" not in body
    assert body["status"] == "done"
    assert body["transcript"] == "What are the benefits of AI?"


# ---------------------------------------------------------------------------
# Generate / retry / regenerate — authorization
# ---------------------------------------------------------------------------
def test_assistant_actions_require_session(client, fake_db):
    # Same-origin (CSRF passes) but no host session -> 401 Unauthorized.
    qid = _seed_done_question(fake_db)
    for action in ("generate", "retry", "regenerate"):
        r = client.post(
            f"/api/v1/admin/questions/{qid}/assistant/{action}", headers={"origin": ORIGIN}
        )
        assert r.status_code == 401, action


def test_assistant_generate_rejects_cross_origin_csrf(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    r = host_client.post(
        f"/api/v1/admin/questions/{qid}/assistant/generate",
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Generate / retry / regenerate — behaviour + idempotency
# ---------------------------------------------------------------------------
def test_generate_creates_queued_job(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    r = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/generate")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert qid in fake_db.assistant_responses


def test_generate_is_idempotent(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    first = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/generate").json()
    second = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/generate").json()
    # Same row id — repeated clicks never create duplicate jobs.
    assert first["id"] == second["id"]
    assert len(fake_db.assistant_responses) == 1


def test_generate_unknown_question_404(host_client, fake_db):
    r = host_client.post("/api/v1/admin/questions/does-not-exist/assistant/generate")
    assert r.status_code == 404


def test_retry_resets_errored_answer(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    fake_db.seed_assistant_response(qid, status="error")
    r = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/retry")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert fake_db.assistant_responses[qid]["status"] == "queued"


def test_retry_leaves_done_answer_untouched(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    fake_db.seed_assistant_response(qid, status="done", response_text="kept")
    r = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/retry")
    # retry only re-runs errors; a completed answer stays completed.
    assert r.status_code == 200
    assert r.json()["status"] == "done"
    assert fake_db.assistant_responses[qid]["response_text"] == "kept"


def test_regenerate_resets_done_answer(host_client, fake_db):
    qid = _seed_done_question(fake_db)
    fake_db.seed_assistant_response(qid, status="done", response_text="old answer")
    r = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/regenerate")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert fake_db.assistant_responses[qid]["response_text"] is None


def test_retry_creates_job_when_missing(host_client, fake_db):
    # Forgiving: retry/regenerate on a never-generated question just enqueues it.
    qid = _seed_done_question(fake_db)
    r = host_client.post(f"/api/v1/admin/questions/{qid}/assistant/regenerate")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
