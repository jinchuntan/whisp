"""Application configuration, loaded from environment (and .env for local dev).

Nothing here contacts Supabase or Agora. Values are read lazily via
``get_settings()`` so importing the app (e.g. in tests) never requires real
credentials.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from persephone_api.models import DEFAULT_TRANSCRIPTION_MODE, TranscriptionMode


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase ---
    # service_role: server/worker only (bypasses RLS). NEVER sent to the browser.
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "persephone-audio"
    # anon / publishable key: used ONLY server-side to call the Supabase Auth REST
    # API for host email/password login. Public by design, but we still keep auth
    # flows on the server so tokens live in HttpOnly cookies, never in JS.
    supabase_anon_key: str = ""

    # --- Badge auth (unchanged) ---
    badge_api_key: str = ""
    badge_id_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"

    # --- Host (dashboard) auth ---
    # Comma-separated, case-insensitive allowlist of admin emails. Only these
    # Supabase users may access host routes.
    admin_email_allowlist: str = ""
    # Session cookies (HttpOnly). Secure MUST be true in production (HTTPS).
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    # Refresh-cookie lifetime; the access cookie tracks the token's own expiry.
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 days

    # Legacy shared admin API key. TEMPORARY compatibility for tests/CLI only.
    # Default OFF; the browser must never use it.
    admin_api_key: str = ""
    allow_legacy_admin_key: bool = False

    # --- Upload validation ---
    max_audio_bytes: int = 4_194_304

    # --- Display-only (the API never transcribes; the worker owns the mode) ---
    transcription_mode: TranscriptionMode = DEFAULT_TRANSCRIPTION_MODE
    worker_offline_seconds: int = 20

    # --- CORS / CSRF origins ---
    # Restrict to real origins in production (e.g. https://whispspace.vercel.app).
    # "*" is dev-only: with credentialed cookies, browsers require explicit origins.
    cors_allow_origins: str = "*"

    @property
    def cors_origin_list(self) -> list[str]:
        raw = self.cors_allow_origins.strip()
        if raw in ("", "*"):
            return ["*"]
        return [o.strip() for o in raw.split(",") if o.strip()]

    @property
    def admin_emails(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_email_allowlist.split(",") if e.strip()}

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def auth_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton (override in tests via dependency_overrides)."""
    return Settings()
