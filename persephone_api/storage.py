"""Private audio storage gateway (Supabase Storage).

Only the API's upload path lives here. The worker has its own download-capable
storage helper. Both share the private ``persephone-audio`` bucket.

A ``Storage`` Protocol is defined so tests inject an in-memory fake and never
touch Supabase.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from supabase import Client


@runtime_checkable
class Storage(Protocol):
    async def upload_wav(self, object_path: str, data: bytes) -> str: ...

    async def create_signed_url(self, object_path: str, expires_in: int = 3600) -> str: ...

    async def remove(self, object_paths: list[str]) -> None: ...


class SupabaseStorage:
    """Storage implementation backed by supabase-py v2 (sync client off-thread)."""

    def __init__(self, client: Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    async def upload_wav(self, object_path: str, data: bytes) -> str:
        def _upload() -> None:
            self._client.storage.from_(self._bucket).upload(
                path=object_path,
                file=data,
                file_options={"content-type": "audio/wav", "upsert": "true"},
            )

        await asyncio.to_thread(_upload)
        return object_path

    async def create_signed_url(self, object_path: str, expires_in: int = 3600) -> str:
        def _sign() -> str:
            res = self._client.storage.from_(self._bucket).create_signed_url(
                object_path, expires_in
            )
            # supabase-py returns {"signedURL": "..."} (key casing varies by version).
            return str(res.get("signedURL") or res.get("signed_url") or "")

        return await asyncio.to_thread(_sign)

    async def remove(self, object_paths: list[str]) -> None:
        if not object_paths:
            return

        def _remove() -> None:
            self._client.storage.from_(self._bucket).remove(object_paths)

        await asyncio.to_thread(_remove)


def get_storage() -> Storage:
    """FastAPI dependency: real Supabase-backed storage (overridden in tests)."""
    from persephone_api._client import get_supabase_client
    from persephone_api.config import get_settings

    return SupabaseStorage(get_supabase_client(), get_settings().supabase_audio_bucket)
