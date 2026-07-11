"""FastAPI application factory for the Whisp web/API control plane.

On Vercel the static dashboard in ``public/`` is served by the CDN and the
Python function only handles ``/api/...``. Locally (``uvicorn main:app``) we also
mount ``public/`` so the whole app runs from one process. Mounting order keeps
API routes ahead of the static catch-all.
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


def _mount_static(app: FastAPI) -> None:
    """Serve the dashboard from ``public/`` for local dev (no-op if absent)."""
    from pathlib import Path

    public_dir = Path(__file__).resolve().parent.parent / "public"
    if not public_dir.is_dir():
        return
    from fastapi.staticfiles import StaticFiles

    # Added last so /api/... routes match first; "/" serves index.html.
    app.mount("/", StaticFiles(directory=str(public_dir), html=True), name="static")


app = create_app()
