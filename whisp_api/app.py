"""FastAPI application factory for the Whisp web/API control plane.

Locally (``uvicorn main:app``) this single process serves BOTH the ``/api/...``
routes and the static dashboard in ``public/`` (via a StaticFiles mount at "/"),
so ``make dev`` shows the dashboard at ``/``. On Vercel, ``public/`` is served
automatically as static assets and a ``vercel.json`` rewrite maps ``/`` to
``/index.html``; only ``/api/*`` reaches this function, so the StaticFiles mount
is a local-dev convenience there. Either way, API routers are registered before
the "/" mount, so ``/api/...`` matches first.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from whisp_api import __version__
from whisp_api.config import get_settings
from whisp_api.models import API_PREFIX
from whisp_api.routes import admin, badge, health, questions

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("whisp.app")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Whisp API",
        version=__version__,
        description="Anonymous voice Q&A for conferences.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Versioned API routers.
    app.include_router(health.router, prefix=API_PREFIX)
    app.include_router(badge.router, prefix=API_PREFIX)
    app.include_router(questions.router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)

    @app.exception_handler(500)
    async def _internal_error(_request: Request, _exc: Exception) -> JSONResponse:
        # Never leak stack traces or internals to clients.
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_error", "message": "Internal server error"},
        )

    _mount_static(app)
    return app


def _find_public_dir() -> os.PathLike[str] | None:
    """Locate the dashboard's ``public/`` directory for the local-dev mount.

    On Vercel the dashboard is served by ``@vercel/static`` (see ``vercel.json``),
    not by this function, so this is primarily for ``make dev``. We still check a
    couple of candidate locations so it is robust to the working directory.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parent.parent / "public",  # repo root
        Path.cwd() / "public",  # when run from the project root
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _mount_static(app: FastAPI) -> None:
    """Mount the dashboard at "/" for local dev (no-op if ``public/`` is absent).

    Registered LAST so the API routers (added first) win for ``/api/...``;
    everything else falls through to the static files, with ``index.html`` served
    for ``/`` (``html=True``). On Vercel this mount is unused — Vercel's static
    layer serves ``public/`` and only ``/api/*`` reaches this function.
    """
    public_dir = _find_public_dir()
    if public_dir is None:
        log.info("public/ not found — dashboard served by Vercel static layer (not this function)")
        return
    from fastapi.staticfiles import StaticFiles

    log.info("serving dashboard from %s", public_dir)
    app.mount("/", StaticFiles(directory=str(public_dir), html=True), name="static")


app = create_app()
