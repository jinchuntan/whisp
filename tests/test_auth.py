"""Authentication: badge (unchanged) + host email/password session (new).

All Supabase Auth calls are faked — no network, no real Supabase.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import (
    BADGE_KEY,
    HOST_EMAIL,
    HOST_PASSWORD,
    ORIGIN,
    OTHER_EMAIL,
    FakeAuthClient,
    FakeDatabase,
    FakeStorage,
    make_wav,
)
from whisp_api.app import create_app
from whisp_api.config import Settings, get_settings
from whisp_api.database import get_database
from whisp_api.storage import get_storage
from whisp_api.supabase_auth import get_auth_client

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
LOGOUT = "/api/v1/auth/logout"
ADMIN_STATE = "/api/v1/admin/state"


def _set_cookie_headers(response) -> str:
    raw = [(k.decode().lower(), v.decode()) for k, v in response.headers.raw]
    return " ".join(v for k, v in raw if k == "set-cookie").lower()


def _make_client(settings: Settings, auth: FakeAuthClient | None = None) -> TestClient:
    app = create_app()
    a = auth or FakeAuthClient()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: FakeDatabase()
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    app.dependency_overrides[get_auth_client] = lambda: a
    c = TestClient(app)
    c.headers.update({"origin": ORIGIN})
    return c


# ---------------------------------------------------------------------------
# Badge auth (unchanged)
# ---------------------------------------------------------------------------
def test_badge_state_requires_key(client):
    assert client.get("/api/v1/badge/state", params={"badge_id": "badge-001"}).status_code == 401


def test_badge_state_rejects_wrong_key(client):
    r = client.get(
        "/api/v1/badge/state", params={"badge_id": "badge-001"}, headers={"X-Whisp-Key": "nope"}
    )
    assert r.status_code == 401


def test_badge_state_accepts_valid_key(client, fake_db):
    fake_db.seed_event()
    r = client.get(
        "/api/v1/badge/state", params={"badge_id": "badge-001"}, headers={"X-Whisp-Key": BADGE_KEY}
    )
    assert r.status_code == 200


def test_badge_upload_requires_key(client):
    r = client.post("/api/v1/questions", content=make_wav(), headers={"X-Badge-Id": "badge-001"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# Host login
# ---------------------------------------------------------------------------
def test_login_success(client):
    r = client.post(
        LOGIN, json={"email": HOST_EMAIL, "password": HOST_PASSWORD}, headers={"origin": ORIGIN}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["email"] == HOST_EMAIL
    # No tokens leak to the client body.
    assert "access_token" not in r.text and "refresh_token" not in r.text


def test_login_wrong_password(client):
    r = client.post(
        LOGIN, json={"email": HOST_EMAIL, "password": "wrong"}, headers={"origin": ORIGIN}
    )
    assert r.status_code == 401
    assert "Incorrect email or password" in r.json()["detail"]
    assert "set-cookie" not in {k.decode().lower() for k, _ in r.headers.raw}


def test_login_unauthorized_email(client):
    # Valid Supabase credentials, but not on the allowlist -> 403, no cookies.
    r = client.post(
        LOGIN, json={"email": OTHER_EMAIL, "password": "also-valid-pw"}, headers={"origin": ORIGIN}
    )
    assert r.status_code == 403
    assert "not authorized" in r.json()["detail"].lower()
    assert "whisp_at" not in _set_cookie_headers(r)


def test_login_service_unavailable_is_503(client, test_settings):
    c = _make_client(test_settings, auth=FakeAuthClient(unavailable=True))
    r = c.post(LOGIN, json={"email": HOST_EMAIL, "password": HOST_PASSWORD})
    assert r.status_code == 503


def test_login_sets_httponly_lax_cookies(client):
    r = client.post(
        LOGIN, json={"email": HOST_EMAIL, "password": HOST_PASSWORD}, headers={"origin": ORIGIN}
    )
    joined = _set_cookie_headers(r)
    assert "whisp_at=" in joined and "whisp_rt=" in joined
    assert "httponly" in joined
    assert "samesite=lax" in joined
    assert "path=/" in joined


def test_secure_flag_present_when_configured():
    settings = Settings(
        supabase_url="https://x.supabase.co",
        supabase_anon_key="anon",
        admin_email_allowlist=HOST_EMAIL,
        cors_allow_origins=ORIGIN,
        session_cookie_secure=True,
    )
    c = _make_client(settings)
    r = c.post(LOGIN, json={"email": HOST_EMAIL, "password": HOST_PASSWORD})
    assert "secure" in _set_cookie_headers(r)


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------
def test_me_unauthenticated(client):
    body = client.get(ME).json()
    assert body == {"authenticated": False, "email": None, "role": None}


def test_me_authenticated(host_client):
    body = host_client.get(ME).json()
    assert body["authenticated"] is True
    assert body["email"] == HOST_EMAIL
    assert body["role"] == "host"


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------
def test_expired_access_refreshes_via_refresh_cookie(host_client):
    # Access token gone (expired), but the valid refresh cookie should
    # transparently mint a new session.
    host_client.cookies.delete("whisp_at")
    r = host_client.get(ME)
    assert r.status_code == 200
    assert r.json()["authenticated"] is True
    # A fresh access cookie was issued.
    assert "whisp_at=" in _set_cookie_headers(r)


def test_logout_clears_cookies_and_revokes(host_client, fake_auth):
    r = host_client.post(LOGOUT)
    assert r.status_code == 200
    assert fake_auth.signed_out, "sign_out should be called"
    # Cookies deleted -> subsequent /me is unauthenticated.
    assert host_client.get(ME).json()["authenticated"] is False


# ---------------------------------------------------------------------------
# Admin route guard
# ---------------------------------------------------------------------------
def test_admin_route_without_session_401(client):
    assert client.get(ADMIN_STATE).status_code == 401


def test_admin_route_with_valid_session_200(host_client, fake_db):
    fake_db.seed_event()
    assert host_client.get(ADMIN_STATE).status_code == 200


def test_admin_post_rejects_bad_origin_csrf(host_client):
    # Logged in, but a cross-origin POST must be rejected.
    r = host_client.post(
        "/api/v1/admin/events", json={"name": "x"}, headers={"origin": "https://evil.example"}
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Legacy API-key compatibility (gated, default OFF)
# ---------------------------------------------------------------------------
def test_legacy_admin_key_disabled_by_default(test_settings):
    c = _make_client(test_settings)  # allow_legacy_admin_key=False
    r = c.get(ADMIN_STATE, headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 401


def test_legacy_admin_key_works_when_enabled():
    settings = Settings(
        supabase_url="https://x.supabase.co",
        supabase_anon_key="anon",
        admin_email_allowlist=HOST_EMAIL,
        cors_allow_origins=ORIGIN,
        admin_api_key="legacy-secret",
        allow_legacy_admin_key=True,
    )
    app = create_app()
    db = FakeDatabase()
    db.seed_event()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_storage] = lambda: FakeStorage()
    app.dependency_overrides[get_auth_client] = lambda: FakeAuthClient()
    c = TestClient(app)
    r = c.get(ADMIN_STATE, headers={"Authorization": "Bearer legacy-secret"})
    assert r.status_code == 200
