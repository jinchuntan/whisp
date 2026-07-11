"""Application configuration, loaded from environment (and .env for local dev).

Nothing here contacts Supabase or Agora. Values are read lazily via
``get_settings()`` so importing the app (e.g. in tests) never requires real
credentials.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from whisp_api.models import DEFAULT_TRANSCRIPTION_MODE, TranscriptionMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase (server-side only) ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "whisp-audio"

    # --- Prototype API keys ---
    badge_api_key: str = ""
    admin_api_key: str = ""

    # --- Upload validation ---
    # Vercel caps request bodies at 4.5 MB; stay at or below that.
    max_audio_bytes: int = 4_194_304
    badge_id_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"

    # --- Display-only (the API never transcribes; the worker owns the mode) ---
    transcription_mode: TranscriptionMode = DEFAULT_TRANSCRIPTION_MODE
    worker_offline_seconds: int = 20

    # --- CORS ---
    cors_allow_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if raw in ("", "*"):
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton (override in tests via dependency_overrides)."""
    return Settings()
