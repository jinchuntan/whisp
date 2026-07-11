"""Host authentication endpoints (email/password via Supabase Auth).

Tokens are set as HttpOnly cookies and never returned to JavaScript. Passwords,
cookies, and tokens are never logged.
"""

from __future__ import annotations

import contextlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from whisp_api.auth import (
    ACCESS_COOKIE,
    AdminIdentity,
    clear_session_cookies,
    enforce_csrf,
    require_admin_user,
    set_session_cookies,
    validate_session,
)
from whisp_api.config import Settings, get_settings
from whisp_api.schemas import AuthMeResponse, LoginRequest, LoginResponse, OkResponse
from whisp_api.supabase_auth import (
    AuthClient,
    AuthUnavailable,
    InvalidCredentials,
    get_auth_client,
)

log = logging.getLogger("whisp.auth")

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthClient = Depends(get_auth_client),
) -> LoginResponse:
    enforce_csrf(request, settings)
    if not settings.auth_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Login is not configured")

    email = body.email.strip()
    try:
        session = await auth.sign_in(email, body.password)
    except InvalidCredentials:
        # Do not reveal whether the email exists.
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        ) from None
    except AuthUnavailable:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="Authentication service unavailable"
        ) from None

    resolved = (session.email or email).strip()
    if resolved.lower() not in settings.admin_emails:
        # Authenticated but not an authorized host. No cookies are set.
        log.info("login denied: not on allowlist")
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="This account is not authorized for host access"
        )

    set_session_cookies(response, session, settings)
    log.info("host login ok")
    return LoginResponse(authenticated=True, email=resolved)


@router.get("/me", response_model=AuthMeResponse)
async def me(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthClient = Depends(get_auth_client),
) -> AuthMeResponse:
    email = await validate_session(request, response, auth, settings)
    if not email or email.lower() not in settings.admin_emails:
        return AuthMeResponse(authenticated=False)
    return AuthMeResponse(authenticated=True, email=email, role="host")


@router.post("/logout", response_model=OkResponse)
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    auth: AuthClient = Depends(get_auth_client),
) -> OkResponse:
    enforce_csrf(request, settings)
    access = request.cookies.get(ACCESS_COOKIE)
    if access:
        # Best-effort revocation; cookies are cleared regardless.
        with contextlib.suppress(Exception):
            await auth.sign_out(access)
    clear_session_cookies(response, settings)
    return OkResponse()


@router.get("/session", response_model=AuthMeResponse)
async def session_probe(identity: AdminIdentity = Depends(require_admin_user)) -> AuthMeResponse:
    """Authenticated probe (used by tests/tools to assert the guard works)."""
    return AuthMeResponse(authenticated=True, email=identity.email, role="host")
