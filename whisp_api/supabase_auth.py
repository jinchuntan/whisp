"""Server-side Supabase Auth (GoTrue) REST client for host email/password login.

We deliberately talk to the Supabase Auth REST API from the server (not the
browser) so access/refresh tokens live only in HttpOnly cookies and never reach
JavaScript. This client uses the *anon* key (public by design) — never the
service_role key — and is separate from the service_role data client.

Nothing here runs at import time; tests inject a fake client, so no real Supabase
call is ever made in the suite.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

# How long a validated access token is trusted without re-contacting Supabase.
# Keeps the 1.5s dashboard poll snappy without hammering the Auth API. Short
# enough that server-side revocation takes effect quickly.
_USER_CACHE_TTL = 15.0


@dataclass
class AuthSession:
    access_token: str
    refresh_token: str
    expires_in: int
    email: str | None = None


@dataclass
class AuthUser:
    id: str
    email: str
    app_metadata: dict[str, Any] = field(default_factory=dict)
    user_metadata: dict[str, Any] = field(default_factory=dict)


class AuthError(Exception):
    """Base auth failure."""


class InvalidCredentials(AuthError):
    """Email/password rejected by Supabase."""


class InvalidSession(AuthError):
    """Access/refresh token is missing, malformed, or expired."""


class AuthUnavailable(AuthError):
    """Supabase Auth could not be reached / returned a server error."""


class AuthClient(Protocol):
    async def sign_in(self, email: str, password: str) -> AuthSession: ...
    async def get_user(self, access_token: str) -> AuthUser: ...
    async def refresh(self, refresh_token: str) -> AuthSession: ...
    async def sign_out(self, access_token: str) -> None: ...


def _user_from_payload(data: dict[str, Any]) -> AuthUser:
    return AuthUser(
        id=str(data.get("id", "")),
        email=str(data.get("email", "")),
        app_metadata=data.get("app_metadata") or {},
        user_metadata=data.get("user_metadata") or {},
    )


class SupabaseAuthClient:
    """Real GoTrue client. HTTP calls via httpx; validated users cached briefly."""

    def __init__(self, base_url: str, anon_key: str, timeout: float = 10.0) -> None:
        self._base = base_url.rstrip("/")
        self._anon = anon_key
        self._timeout = timeout
        self._user_cache: dict[str, tuple[AuthUser, float]] = {}

    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        headers = {"apikey": self._anon, "Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    async def sign_in(self, email: str, password: str) -> AuthSession:
        import httpx

        url = f"{self._base}/auth/v1/token?grant_type=password"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), json={"email": email, "password": password}
                )
        except httpx.HTTPError as exc:
            raise AuthUnavailable("Could not reach the authentication service") from exc

        if resp.status_code == 200:
            data = resp.json()
            user = data.get("user") or {}
            return AuthSession(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                expires_in=int(data.get("expires_in", 3600)),
                email=user.get("email"),
            )
        if resp.status_code in (400, 401, 403):
            raise InvalidCredentials("Invalid email or password")
        raise AuthUnavailable(f"Authentication service error ({resp.status_code})")

    async def get_user(self, access_token: str) -> AuthUser:
        import httpx

        cached = self._user_cache.get(access_token)
        if cached and cached[1] > time.monotonic():
            return cached[0]

        url = f"{self._base}/auth/v1/user"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers(access_token))
        except httpx.HTTPError as exc:
            raise AuthUnavailable("Could not reach the authentication service") from exc

        if resp.status_code == 200:
            user = _user_from_payload(resp.json())
            self._user_cache[access_token] = (user, time.monotonic() + _USER_CACHE_TTL)
            return user
        if resp.status_code in (401, 403):
            raise InvalidSession("Session is invalid or expired")
        raise AuthUnavailable(f"Authentication service error ({resp.status_code})")

    async def refresh(self, refresh_token: str) -> AuthSession:
        import httpx

        url = f"{self._base}/auth/v1/token?grant_type=refresh_token"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), json={"refresh_token": refresh_token}
                )
        except httpx.HTTPError as exc:
            raise AuthUnavailable("Could not reach the authentication service") from exc

        if resp.status_code == 200:
            data = resp.json()
            user = data.get("user") or {}
            return AuthSession(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", refresh_token),
                expires_in=int(data.get("expires_in", 3600)),
                email=user.get("email"),
            )
        if resp.status_code in (400, 401, 403):
            raise InvalidSession("Refresh token is invalid or expired")
        raise AuthUnavailable(f"Authentication service error ({resp.status_code})")

    async def sign_out(self, access_token: str) -> None:
        import httpx

        self._user_cache.pop(access_token, None)
        url = f"{self._base}/auth/v1/logout"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                await client.post(url, headers=self._headers(access_token))
        except httpx.HTTPError:
            # Best-effort revocation; cookies are cleared regardless.
            return


@lru_cache
def get_auth_client() -> AuthClient:
    """FastAPI dependency: real Supabase Auth client (overridden in tests)."""
    from whisp_api.config import get_settings

    settings = get_settings()
    return SupabaseAuthClient(settings.supabase_url, settings.supabase_anon_key)
