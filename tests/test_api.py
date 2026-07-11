"""End-to-end API behaviour against in-memory fakes."""

from __future__ import annotations

from tests.conftest import make_wav


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "whisp-api"
    assert body["supabase_configured"] is False


def test_health_does_not_touch_agora_or_db(client, fake_db):
    # Health must not require any DB rows or provider config.
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert fake_db.badges == {}


def test_upload_returns_202_and_queues(client, fake_db, fake_storage, badge_headers):
    fake_db.seed_event()
    r = client.post("/api/v1/questions", content=make_wav(), headers=badge_headers)
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "queued"
    qid = body["question_id"]
    assert body["poll_url"] == f"/api/v1/questions/{qid}"
    # Audio stored privately, question persisted as queued.
    assert len(fake_storage.objects) == 1
    assert fake_db.questions[qid]["status"] == "queued"


def test_upload_associates_open_round(client, fake_db, badge_headers):
    ev = fake_db.seed_event()
    rnd = fake_db.seed_round(ev["id"])
    r = client.post("/api/v1/questions", content=make_wav(), headers=badge_headers)
    qid = r.json()["question_id"]
    assert fake_db.questions[qid]["round_id"] == rnd["id"]


def test_upload_rejects_bad_round_header(client, fake_db, badge_headers):
    fake_db.seed_event()
    headers = {**badge_headers, "X-Round-Id": "00000000-0000-0000-0000-000000000000"}
    r = client.post("/api/v1/questions", content=make_wav(), headers=headers)
    assert r.status_code == 400


def test_upload_rejects_non_wav(client, fake_db, badge_headers):
    fake_db.seed_event()
    r = client.post("/api/v1/questions", content=b"not audio at all", headers=badge_headers)
    assert r.status_code == 400
    assert "Invalid WAV" in r.json()["detail"]


def test_upload_rejects_oversized(client, fake_db, badge_headers):
    fake_db.seed_event()
    big = make_wav(seconds=1.0) + b"\x00" * (5 * 1024 * 1024)
    r = client.post("/api/v1/questions", content=big, headers=badge_headers)
    assert r.status_code == 413


def test_upload_rejects_bad_badge_id(client, fake_db):
    fake_db.seed_event()
    headers = {"X-Whisp-Key": "test-badge-key", "X-Badge-Id": "bad id!"}
    r = client.post("/api/v1/questions", content=make_wav(), headers=headers)
    assert r.status_code == 400


def test_upload_storage_failure_is_safe(client, fake_db, fake_storage, badge_headers):
    fake_db.seed_event()
    fake_storage.fail = True
    r = client.post("/api/v1/questions", content=make_wav(), headers=badge_headers)
    assert r.status_code == 502
    assert "message" not in r.json() or "trace" not in r.text.lower()


def test_badge_state_reports_round(client, fake_db):
    ev = fake_db.seed_event("Conf")
    rnd = fake_db.seed_round(ev["id"], "Ask me anything")
    r = client.get(
        "/api/v1/badge/state",
        params={"badge_id": "badge-001"},
        headers={"X-Whisp-Key": "test-badge-key"},
    )
    body = r.json()
    assert body["event_name"] == "Conf"
    assert body["round_id"] == rnd["id"]
    assert body["round_prompt"] == "Ask me anything"
    assert body["accepting"] is True


def test_badge_state_notifications(client, fake_db):
    ev = fake_db.seed_event()
    rnd = fake_db.seed_round(ev["id"])
    cl = fake_db.seed_cluster(rnd["id"], "How does AI help?", count=3)
    q = {
        "id": "q1",
        "badge_id": "badge-001",
        "status": "done",
        "cluster_id": cl["id"],
        "round_id": rnd["id"],
    }
    fake_db.questions["q1"] = q
    r = client.get(
        "/api/v1/badge/state",
        params={"badge_id": "badge-001"},
        headers={"X-Whisp-Key": "test-badge-key"},
    )
    notes = r.json()["notifications"]
    assert len(notes) == 1
    assert notes[0]["similar_count"] == 3


def test_admin_create_event_and_round(client, admin_headers):
    r = client.post("/api/v1/admin/events", json={"name": "My Conf"}, headers=admin_headers)
    assert r.status_code == 201
    event_id = r.json()["id"]
    assert len(r.json()["join_code"]) == 6

    r2 = client.post(
        "/api/v1/admin/rounds",
        json={"prompt": "What's your question?", "event_id": event_id},
        headers=admin_headers,
    )
    assert r2.status_code == 201
    assert r2.json()["status"] == "open"


def test_admin_open_round_closes_previous(client, fake_db, admin_headers):
    ev = fake_db.seed_event()
    first = fake_db.seed_round(ev["id"], "first")
    r = client.post(
        "/api/v1/admin/rounds",
        json={"prompt": "second", "event_id": ev["id"]},
        headers=admin_headers,
    )
    assert r.status_code == 201
    first_row = next(x for x in fake_db.rounds if x["id"] == first["id"])
    assert first_row["status"] == "closed"


def test_admin_close_round(client, fake_db, admin_headers):
    ev = fake_db.seed_event()
    rnd = fake_db.seed_round(ev["id"])
    r = client.post(f"/api/v1/admin/rounds/{rnd['id']}/close", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


def test_admin_close_unknown_round_404(client, admin_headers):
    r = client.post("/api/v1/admin/rounds/nope/close", headers=admin_headers)
    assert r.status_code == 404


def test_admin_mark_question_answered(client, fake_db, admin_headers):
    ev = fake_db.seed_event()
    fake_db.questions["qX"] = {"id": "qX", "badge_id": "b", "status": "done", "event_id": ev["id"]}
    r = client.post("/api/v1/admin/questions/qX/answered", headers=admin_headers)
    assert r.status_code == 200
    assert fake_db.questions["qX"]["answered_at"] is not None


def test_admin_mark_cluster_answered(client, fake_db, admin_headers):
    ev = fake_db.seed_event()
    rnd = fake_db.seed_round(ev["id"])
    cl = fake_db.seed_cluster(rnd["id"], "canon", 2)
    r = client.post(f"/api/v1/admin/clusters/{cl['id']}/answered", headers=admin_headers)
    assert r.status_code == 200
    assert fake_db.clusters[cl["id"]]["status"] == "answered"


def test_admin_recluster(client, fake_db, admin_headers):
    ev = fake_db.seed_event()
    fake_db.seed_round(ev["id"])
    r = client.post("/api/v1/admin/recluster", headers=admin_headers)
    assert r.status_code == 200


def test_admin_recluster_no_round_400(client, fake_db, admin_headers):
    fake_db.seed_event()
    r = client.post("/api/v1/admin/recluster", headers=admin_headers)
    assert r.status_code == 400


def test_worker_health_online_offline(client, fake_db, admin_headers):
    from datetime import datetime, timedelta, timezone

    fake_db.seed_heartbeat("worker-a", "faster_whisper_only")  # fresh -> online
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    fake_db.seed_heartbeat("worker-b", "agora_first", last_seen=old)  # stale -> offline

    r = client.get("/api/v1/admin/worker-health", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["worker_online"] is True
    by_id = {w["worker_id"]: w for w in body["workers"]}
    assert by_id["worker-a"]["online"] is True
    assert by_id["worker-b"]["online"] is False


def test_admin_state_shape(client, fake_db, admin_headers):
    ev = fake_db.seed_event("Conf")
    rnd = fake_db.seed_round(ev["id"], "prompt")
    fake_db.seed_cluster(rnd["id"], "canon", 2)
    r = client.get("/api/v1/admin/state", headers=admin_headers)
    body = r.json()
    assert body["transcription_mode"] == "faster_whisper_only"
    assert body["agora_mode_active"] is False
    assert body["event"]["name"] == "Conf"
    assert body["open_round"]["prompt"] == "prompt"
    assert len(body["clusters"]) == 1
