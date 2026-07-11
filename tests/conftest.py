"""Shared test fixtures: in-memory fakes for the DB and storage so the API test
suite never contacts Supabase (or the network)."""

from __future__ import annotations

import struct
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from whisp_api.app import create_app
from whisp_api.config import Settings, get_settings
from whisp_api.database import get_database
from whisp_api.storage import get_storage
from whisp_api.supabase_auth import (
    AuthSession,
    AuthUnavailable,
    AuthUser,
    InvalidCredentials,
    InvalidSession,
    get_auth_client,
)

BADGE_KEY = "test-badge-key"
ADMIN_KEY = "test-admin-key"

HOST_EMAIL = "host@example.com"
HOST_PASSWORD = "correct-horse-battery-staple"
OTHER_EMAIL = "outsider@example.com"  # valid Supabase user, NOT on the allowlist
ORIGIN = "http://testserver"  # TestClient's origin; matches CORS_ALLOW_ORIGINS below


def make_wav(seconds: float = 0.2, sample_rate: int = 16000) -> bytes:
    """Build a valid mono PCM16 WAV of silence for upload tests."""
    n = int(seconds * sample_rate)
    pcm = b"\x00\x00" * n
    data_size = len(pcm)
    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)
    header += b"WAVE"
    header += b"fmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data"
    header += struct.pack("<I", data_size)
    return header + pcm


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    async def upload_wav(self, object_path: str, data: bytes) -> str:
        if self.fail:
            raise RuntimeError("storage down")
        self.objects[object_path] = data
        return object_path

    async def create_signed_url(self, object_path: str, expires_in: int = 3600) -> str:
        return f"https://signed.example/{object_path}?exp={expires_in}"

    async def remove(self, object_paths: list[str]) -> None:
        for p in object_paths:
            self.objects.pop(p, None)


class FakeDatabase:
    """Minimal in-memory implementation of the Database protocol."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.rounds: list[dict[str, Any]] = []
        self.questions: dict[str, dict[str, Any]] = {}
        self.clusters: dict[str, dict[str, Any]] = {}
        self.badges: dict[str, dict[str, Any]] = {}
        self.heartbeats: list[dict[str, Any]] = []

    # -- seed helpers (tests only) -----------------------------------------
    def seed_event(self, name: str = "Demo", active: bool = True) -> dict[str, Any]:
        ev = {
            "id": str(uuid.uuid4()),
            "name": name,
            "join_code": "ABC123",
            "active": active,
            "created_at": _now_iso(),
        }
        self.events.append(ev)
        return ev

    def seed_round(self, event_id: str, prompt: str = "What next?", status: str = "open") -> dict:
        rnd = {
            "id": str(uuid.uuid4()),
            "event_id": event_id,
            "prompt": prompt,
            "status": status,
            "opened_at": _now_iso(),
            "closed_at": None,
        }
        self.rounds.append(rnd)
        return rnd

    def seed_cluster(self, round_id: str, canonical: str, count: int) -> dict[str, Any]:
        cl = {
            "id": str(uuid.uuid4()),
            "round_id": round_id,
            "canonical_question": canonical,
            "question_count": count,
            "status": "open",
            "created_at": _now_iso(),
            "answered_at": None,
        }
        self.clusters[cl["id"]] = cl
        return cl

    def seed_heartbeat(self, worker_id: str, mode: str, last_seen: str | None = None) -> dict:
        hb = {
            "worker_id": worker_id,
            "version": "0.1.0",
            "transcription_mode": mode,
            "status": "idle",
            "last_seen_at": last_seen or _now_iso(),
        }
        self.heartbeats.append(hb)
        return hb

    # -- events ------------------------------------------------------------
    async def get_active_event(self) -> dict[str, Any] | None:
        active = [e for e in self.events if e.get("active")]
        return active[-1] if active else None

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e["id"] == event_id), None)

    async def create_event(self, name: str, join_code: str) -> dict[str, Any]:
        ev = {
            "id": str(uuid.uuid4()),
            "name": name,
            "join_code": join_code,
            "active": True,
            "created_at": _now_iso(),
        }
        self.events.append(ev)
        return ev

    async def deactivate_events_except(self, event_id: str) -> None:
        for e in self.events:
            if e["id"] != event_id:
                e["active"] = False

    # -- rounds ------------------------------------------------------------
    async def get_open_round(self, event_id: str) -> dict[str, Any] | None:
        rs = [r for r in self.rounds if r["event_id"] == event_id and r["status"] == "open"]
        return rs[-1] if rs else None

    async def get_round(self, round_id: str) -> dict[str, Any] | None:
        return next((r for r in self.rounds if r["id"] == round_id), None)

    async def list_rounds(self, event_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return [r for r in self.rounds if r["event_id"] == event_id][:limit]

    async def create_round(self, event_id: str, prompt: str | None) -> dict[str, Any]:
        return self.seed_round(event_id, prompt or "", status="open")

    async def close_open_rounds(self, event_id: str) -> None:
        for r in self.rounds:
            if r["event_id"] == event_id and r["status"] == "open":
                r["status"] = "closed"
                r["closed_at"] = _now_iso()

    async def close_round(self, round_id: str) -> dict[str, Any] | None:
        r = await self.get_round(round_id)
        if r:
            r["status"] = "closed"
            r["closed_at"] = _now_iso()
        return r

    async def request_recluster(self, round_id: str) -> bool:
        r = await self.get_round(round_id)
        if r:
            r["recluster_requested_at"] = _now_iso()
            return True
        return False

    # -- badges ------------------------------------------------------------
    async def touch_badge(self, badge_id: str, event_id: str | None) -> None:
        self.badges[badge_id] = {"id": badge_id, "event_id": event_id, "last_seen_at": _now_iso()}

    # -- questions ---------------------------------------------------------
    async def create_question(
        self, *, event_id, round_id, badge_id, audio_storage_path
    ) -> dict[str, Any]:
        q = {
            "id": str(uuid.uuid4()),
            "event_id": event_id,
            "round_id": round_id,
            "badge_id": badge_id,
            "audio_storage_path": audio_storage_path,
            "status": "queued",
            "transcript": None,
            "provider_used": None,
            "fallback_used": False,
            "processing_ms": None,
            "language": None,
            "cluster_id": None,
            "safe_error_message": None,
            "created_at": _now_iso(),
            "answered_at": None,
        }
        self.questions[q["id"]] = q
        return q

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        return self.questions.get(question_id)

    async def list_recent_questions(self, event_id=None, limit: int = 50) -> list[dict[str, Any]]:
        rows = list(self.questions.values())
        if event_id:
            rows = [q for q in rows if q.get("event_id") == event_id]
        return rows[:limit]

    async def get_badge_notifications(self, badge_id, round_id) -> list[dict[str, Any]]:
        out = []
        for q in self.questions.values():
            if q["badge_id"] != badge_id or q["status"] != "done" or not q.get("cluster_id"):
                continue
            if round_id and q.get("round_id") != round_id:
                continue
            cl = self.clusters.get(q["cluster_id"])
            count = int(cl["question_count"]) if cl else 1
            if count > 1:
                out.append(
                    {
                        "question_id": q["id"],
                        "cluster_id": q["cluster_id"],
                        "similar_count": count,
                        "canonical_question": cl["canonical_question"] if cl else None,
                    }
                )
        return out

    async def mark_question_answered(self, question_id: str) -> bool:
        q = self.questions.get(question_id)
        if q:
            q["answered_at"] = _now_iso()
            return True
        return False

    # -- clusters ----------------------------------------------------------
    async def get_cluster(self, cluster_id: str) -> dict[str, Any] | None:
        return self.clusters.get(cluster_id)

    async def list_clusters(self, round_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = [c for c in self.clusters.values() if c["round_id"] == round_id]
        return sorted(rows, key=lambda c: c["question_count"], reverse=True)[:limit]

    async def mark_cluster_answered(self, cluster_id: str) -> bool:
        c = self.clusters.get(cluster_id)
        if c:
            c["status"] = "answered"
            c["answered_at"] = _now_iso()
            return True
        return False

    # -- heartbeats --------------------------------------------------------
    async def list_worker_heartbeats(self) -> list[dict[str, Any]]:
        return list(self.heartbeats)


class FakeAuthClient:
    """In-memory Supabase Auth stand-in. No network; deterministic."""

    def __init__(self, unavailable: bool = False) -> None:
        # email -> password (both HOST and OTHER have valid credentials; only
        # HOST is on the allowlist, so OTHER exercises the "unauthorized" path).
        self.passwords = {HOST_EMAIL: HOST_PASSWORD, OTHER_EMAIL: "also-valid-pw"}
        self.access_to_email: dict[str, str] = {}
        self.refresh_to_email: dict[str, str] = {}
        self.signed_out: list[str] = []
        self.unavailable = unavailable
        self._n = 0

    def _issue(self, email: str) -> AuthSession:
        self._n += 1
        at, rt = f"at-{email}-{self._n}", f"rt-{email}-{self._n}"
        self.access_to_email[at] = email
        self.refresh_to_email[rt] = email
        return AuthSession(access_token=at, refresh_token=rt, expires_in=3600, email=email)

    async def sign_in(self, email: str, password: str) -> AuthSession:
        if self.unavailable:
            raise AuthUnavailable("down")
        if self.passwords.get(email) != password:
            raise InvalidCredentials("bad creds")
        return self._issue(email)

    async def get_user(self, access_token: str) -> AuthUser:
        if self.unavailable:
            raise AuthUnavailable("down")
        email = self.access_to_email.get(access_token)
        if not email:
            raise InvalidSession("unknown token")
        return AuthUser(id=f"uid-{email}", email=email)

    async def refresh(self, refresh_token: str) -> AuthSession:
        if self.unavailable:
            raise AuthUnavailable("down")
        email = self.refresh_to_email.get(refresh_token)
        if not email:
            raise InvalidSession("unknown refresh")
        self._n += 1
        at = f"at-{email}-{self._n}"
        self.access_to_email[at] = email
        return AuthSession(
            access_token=at, refresh_token=refresh_token, expires_in=3600, email=email
        )

    async def sign_out(self, access_token: str) -> None:
        self.access_to_email.pop(access_token, None)
        self.signed_out.append(access_token)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        badge_api_key=BADGE_KEY,
        admin_api_key=ADMIN_KEY,
        supabase_url="https://test.supabase.co",
        supabase_anon_key="test-anon-key",
        supabase_service_role_key="",
        admin_email_allowlist=f"{HOST_EMAIL}, Someone-Else@Example.com",
        session_cookie_secure=False,  # tests run over http
        cors_allow_origins=ORIGIN,
        allow_legacy_admin_key=False,
    )


@pytest.fixture
def fake_db() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fake_auth() -> FakeAuthClient:
    return FakeAuthClient()


@pytest.fixture
def app(test_settings, fake_db, fake_storage, fake_auth):
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings
    application.dependency_overrides[get_database] = lambda: fake_db
    application.dependency_overrides[get_storage] = lambda: fake_storage
    application.dependency_overrides[get_auth_client] = lambda: fake_auth
    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def host_client(app) -> TestClient:
    """A TestClient already logged in as the allowlisted host (session cookies)."""
    c = TestClient(app)
    c.headers.update({"origin": ORIGIN})  # satisfies CSRF on state-changing calls
    r = c.post("/api/v1/auth/login", json={"email": HOST_EMAIL, "password": HOST_PASSWORD})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def badge_headers() -> dict[str, str]:
    return {"X-Whisp-Key": BADGE_KEY, "X-Badge-Id": "badge-001", "Content-Type": "audio/wav"}
