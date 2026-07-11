"""Vercel / ASGI entrypoint for the Persephone web + API control plane.

Vercel auto-detects FastAPI and imports the module-level ``app`` from a root
entrypoint (``main.py``). Locally, run ``uvicorn main:app --reload``.
"""

from persephone_api.app import app

__all__ = ["app"]
