"""FastAPI application factory for the Whisp web/API control plane.

The app serves BOTH the ``/api/...`` routes and the static dashboard in
``public/``. On Vercel all requests are routed to this function (``public/`` is
bundled via ``vercel.json`` ``includeFiles``), and locally (``uvicorn main:app``)
the same single process serves everything. Mounting order keeps the API routers
(added first) ahead of the static catch-all mounted at "/".
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
    """Locate the dashboard's ``public/`` directory.

    Serves both local dev and Vercel: on Vercel the app runs from the project
    root (working dir) and ``public/`` is bundled into the function via
    ``vercel.json`` ``includeFiles``. We check a few candidate locations so the
    lookup is robust to how the function is laid out on disk.
    """
    from pathlib import Path

    candidates = [
        Path(__file__).resolve().parent.parent / "public",  # repo/function root
        Path.cwd() / "public",  # Vercel working dir is the project root
        Path("/var/task/public"),  # Vercel Lambda task root (belt-and-suspenders)
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def _mount_static(app: FastAPI) -> None:
    """Serve the dashboard from ``public/`` (local dev AND Vercel).

    Vercel routes all non-static requests to the function, so FastAPI serves the
    dashboard itself. Mounted at "/" LAST, so the API routers (added first) win
    for ``/api/...``; everything else falls through to the static files, with
    ``index.html`` served for ``/`` (``html=True``).
    """
    public_dir = _find_public_dir()
    if public_dir is None:
        log.warning("public/ not found — dashboard will not be served by the API")
        return
    from fastapi.staticfiles import StaticFiles

    log.info("serving dashboard from %s", public_dir)
    app.mount("/", StaticFiles(directory=str(public_dir), html=True), name="static")


app = create_app()
