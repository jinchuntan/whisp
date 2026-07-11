"""Authentication dependencies and session-cookie helpers.

Two independent auth schemes:

* **Badge** (unchanged): ESP32 badges send ``X-Whisp-Key: <BADGE_API_KEY>``.
  Header-based, not browser-ambient, so it is CSRF-safe.
* **Host** (new): the dashboard logs in with email/password (Supabase Auth). The
  server sets HttpOnly access/refresh cookies; ``require_admin_user`` validates
  the session and checks the email allowlist. Tokens never reach JavaScript.

A legacy shared ``ADMIN_API_KEY`` (``Authorization: Bearer``) is retained ONLY
for tests/CLI, gated behind ``ALLOW_LEGACY_ADMIN_KEY`` (default off). The browser
never uses it.

Nothing here logs passwords, cookies, or tokens.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import Depends, Header, HTTPException, Request, Response, status

from whisp_api.config import Settings, get_settings
from whisp_api.supabase_auth import (
    AuthClient,
    AuthSession,
    AuthUnavailable,
    InvalidSession,
    get_auth_client,
)

BADGE_HEADER = "X-Whisp-Key"
ACCESS_COOKIE = "whisp_at"
REFRESH_COOKIE = "whisp_rt"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class AdminIdentity:
    email: str
    via: str  # "session" | "legacy_api_key"


def _safe_equal(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# Badge auth (unchanged)
# ---------------------------------------------------------------------------
async def require_badge_key(
    x_whisp_key: str | None = Header(default=None, alias=BADGE_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    if not _safe_equal(x_whisp_key, settings.badge_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing badge key",
        )


# ---------------------------------------------------------------------------
# Session cookies
# ---------------------------------------------------------------------------
_SameSite = Literal["lax", "strict", "none"]


def _samesite(settings: Settings) -> _SameSite:
    value = settings.session_cookie_samesite.strip().lower()
    if value in ("lax", "strict", "none"):
        return cast(_SameSite, value)
    return "lax"


def set_session_cookies(response: Response, session: AuthSession, settings: Settings) -> None:
    samesite = _samesite(settings)
    secure = settings.session_cookie_secure
    # Access cookie lives as long as the token; refresh cookie longer so the
    # session survives access-token expiry.
    response.set_cookie(
        ACCESS_COOKIE,
        session.access_token,
        max_age=max(session.expires_in, 60),
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    if session.refresh_token:
        response.set_cookie(
            REFRESH_COOKIE,
            session.refresh_token,
            max_age=settings.session_max_age_seconds,
            httponly=True,
            secure=secure,
            samesite=samesite,
            path="/",
        )


def clear_session_cookies(response: Response, settings: Settings) -> None:
    samesite = _samesite(settings)
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(
            name,
            path="/",
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite=samesite,
        )


# ---------------------------------------------------------------------------
# CSRF (for cookie-authenticated, state-changing requests)
# ---------------------------------------------------------------------------
def _allowed_origin_candidates(request: Request, settings: Settings) -> set[str]:
    allowed = settings.cors_origin_list
    if allowed != ["*"]:
        return {o.rstrip("/") for o in allowed}
    # No explicit allowlist -> enforce same-origin against our own host.
    host = request.headers.get("host")
    if not host:
        return set()
    return {f"http://{host}", f"https://{host}"}


def enforce_csrf(request: Request, settings: Settings) -> None:
    """Reject cross-origin state-changing cookie requests (Origin/Referer check)."""
    if request.method in _SAFE_METHODS:
        return
    candidates = _allowed_origin_candidates(request, settings)
    origin = request.headers.get("origin")
    if origin:
        if origin.rstrip("/") in candidates:
            return
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cross-origin request rejected")
    referer = request.headers.get("referer")
    if referer and any(referer.startswith(c) for c in candidates):
        return
    raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Missing or invalid request origin")


# ---------------------------------------------------------------------------
# Host session validation
# ---------------------------------------------------------------------------
async def validate_session(
    request: Request,
    response: Response,
    auth: AuthClient,
    settings: Settings,
) -> str | None:
    """Return the authenticated email, refreshing + re-setting cookies if needed.

    Returns None when there is no usable session. Raises 503 if Supabase Auth is
    unreachable.
    """
    access = request.cookies.get(ACCESS_COOKIE)
    refresh = request.cookies.get(REFRESH_COOKIE)

    if access:
        try:
            user = await auth.get_user(access)
            return user.email
        except InvalidSession:
            pass
        except AuthUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Auth service unavailable"
            ) from exc

    if refresh:
        try:
            session = await auth.refresh(refresh)
        except InvalidSession:
            return None
        except AuthUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Auth service unavailable"
            ) from exc
        set_session_cookies(response, session, settings)
        try:
            user = await auth.get_user(session.access_token)
            return user.email
        except InvalidSession:
            return None
        except AuthUnavailable as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "Auth service unavailable"
            ) from exc

    return None


async def require_admin_user(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthClient = Depends(get_auth_client),
) -> AdminIdentity:
    # 1) Legacy shared key (header auth, CSRF-safe). Gated, default OFF.
    if settings.allow_legacy_admin_key:
        authorization = request.headers.get("authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization[len("Bearer ") :].strip()
            if _safe_equal(token, settings.admin_api_key):
                return AdminIdentity(email="legacy-api-key", via="legacy_api_key")

    # 2) Cookie session. Enforce CSRF for state-changing requests first.
    enforce_csrf(request, settings)

    email = await validate_session(request, response, auth, settings)
    if not email:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Cookie"},
        )
    if email.lower() not in settings.admin_emails:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="This account is not authorized for host access"
        )
    return AdminIdentity(email=email, via="session")
