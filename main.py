"""Vercel / ASGI entrypoint for the Whisp web + API control plane.

Vercel auto-detects FastAPI and imports the module-level ``app`` from a root
entrypoint (``main.py``). Locally, run ``uvicorn main:app --reload``.
"""

from whisp_api.app import app

__all__ = ["app"]
