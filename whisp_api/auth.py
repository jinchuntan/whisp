"""Authentication dependencies.

PROTOTYPE / HACKATHON auth: two shared secrets.
  * Badges send   ``X-Whisp-Key: <BADGE_API_KEY>``.
  * Admin/dashboard sends ``Authorization: Bearer <ADMIN_API_KEY>``.

Keys are compared with ``hmac.compare_digest`` (constant time). Raw header
values are never logged. The roadmap (see README security section) is per-badge
credentials and a real admin session — this is deliberately minimal.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status

from whisp_api.config import Settings, get_settings

BADGE_HEADER = "X-Whisp-Key"
ADMIN_SCHEME = "Bearer"


def _safe_equal(provided: str | None, expected: str) -> bool:
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_badge_key(
    x_whisp_key: str | None = Header(default=None, alias=BADGE_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    if not _safe_equal(x_whisp_key, settings.badge_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing badge key",
        )


async def require_admin_key(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    token: str | None = None
    if authorization and authorization.startswith(f"{ADMIN_SCHEME} "):
        token = authorization[len(ADMIN_SCHEME) + 1 :].strip()
    if not _safe_equal(token, settings.admin_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin key",
            headers={"WWW-Authenticate": "Bearer"},
        )
