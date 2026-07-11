"""Badge and admin authentication."""

from __future__ import annotations

from tests.conftest import ADMIN_KEY, BADGE_KEY, make_wav


def test_badge_state_requires_key(client):
    r = client.get("/api/v1/badge/state", params={"badge_id": "badge-001"})
    assert r.status_code == 401


def test_badge_state_rejects_wrong_key(client):
    r = client.get(
        "/api/v1/badge/state",
        params={"badge_id": "badge-001"},
        headers={"X-Whisp-Key": "nope"},
    )
    assert r.status_code == 401


def test_badge_state_accepts_valid_key(client, fake_db):
    fake_db.seed_event()
    r = client.get(
        "/api/v1/badge/state",
        params={"badge_id": "badge-001"},
        headers={"X-Whisp-Key": BADGE_KEY},
    )
    assert r.status_code == 200


def test_upload_requires_badge_key(client):
    r = client.post("/api/v1/questions", content=make_wav(), headers={"X-Badge-Id": "badge-001"})
    assert r.status_code == 401


def test_admin_state_requires_bearer(client):
    assert client.get("/api/v1/admin/state").status_code == 401


def test_admin_state_rejects_wrong_bearer(client):
    r = client.get("/api/v1/admin/state", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_admin_state_rejects_non_bearer_scheme(client):
    r = client.get("/api/v1/admin/state", headers={"Authorization": f"Basic {ADMIN_KEY}"})
    assert r.status_code == 401


def test_admin_state_accepts_valid_bearer(client):
    r = client.get("/api/v1/admin/state", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
    assert r.status_code == 200
