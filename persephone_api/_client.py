"""Lazily-constructed, cached Supabase client (service_role).

Imported only when a request actually needs the database/storage, so tests that
override the gateway dependencies never construct a real client or require the
``supabase`` package's network layer.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from persephone_api.config import get_settings

if TYPE_CHECKING:
    from supabase import Client


@lru_cache
def get_supabase_client() -> Client:
    from supabase import create_client

    settings = get_settings()
    if not settings.supabase_configured:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY (see .env.example)."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
