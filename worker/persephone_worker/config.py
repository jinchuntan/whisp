"""Worker configuration (env / .env). Never contacts any service on import."""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from persephone_worker.providers.router import MODE_ORDER

# Chatbot provider selection is independent of TRANSCRIPTION_MODE (transcription
# and answer generation are decoupled).
CHATBOT_MODES = frozenset({"disabled", "mock", "ollama", "openai_compatible"})


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "persephone-audio"

    # --- Transcription mode (authoritative selector) ---
    transcription_mode: str = "faster_whisper_only"

    # --- Faster-Whisper ---
    faster_whisper_model: str = "base"
    faster_whisper_device: str = "cpu"
    faster_whisper_compute_type: str = "int8"
    faster_whisper_beam_size: int = 5
    transcription_language: str = "en"

    # --- Agora (only used when the mode selects Agora) ---
    # Secrets — worker-only, never sent to the browser/badge, never logged.
    agora_app_id: str = ""
    agora_app_certificate: str = ""
    agora_customer_id: str = ""
    agora_customer_secret: str = ""
    # Hard master switch. Live RTC/STT calls REFUSE to run unless this is true,
    # even when a mode selects Agora and credentials are present.
    agora_live_enabled: bool = False
    # RTC UIDs. The worker publishes badge audio as agora_worker_uid; Agora's STT
    # bots join as the sub/pub bot UIDs.
    agora_worker_uid: int = 20001
    agora_sub_bot_uid: int = 10001
    agora_pub_bot_uid: int = 10002
    # Short-lived RTC token TTL (seconds).
    agora_token_ttl_seconds: int = 300
    # Credit safety: cap the seconds of audio published, the overall task timeout,
    # the REST idle time, and the number of live Agora jobs per UTC day.
    agora_max_duration_seconds: int = 12
    agora_idle_seconds: int = 10
    agora_daily_max_jobs: int = 20
    # Canary hard cap (seconds) — the manual live-test tool never exceeds this.
    agora_canary_max_seconds: int = 3

    # --- Provider timeouts (seconds) ---
    faster_whisper_timeout: float = 120.0
    agora_timeout: float = 30.0

    # --- Clustering ---
    enable_clustering: bool = True
    cluster_similarity_threshold: float = 0.78
    embedding_model: str = "all-MiniLM-L6-v2"

    # --- Chatbot / voice assistant ---
    # Independent of TRANSCRIPTION_MODE and Agora. 'disabled' does zero chatbot
    # work and makes zero chatbot network calls. See providers/../chatbot/.
    #   disabled | mock | ollama | openai_compatible
    chatbot_mode: str = "disabled"
    chatbot_auto_generate: bool = True
    chatbot_model: str = ""
    chatbot_base_url: str = ""
    # Secret — worker-only. Never sent to the browser/badge; never logged.
    chatbot_api_key: str = ""
    chatbot_timeout_seconds: float = 30.0
    chatbot_max_output_tokens: int = 180
    chatbot_temperature: float = 0.3
    chatbot_max_attempts: int = 3
    # Answer-loop pacing + lease (kept separate from the transcription loop so a
    # slow LLM never blocks or extends transcription leases).
    chatbot_lease_seconds: int = 90
    chatbot_poll_interval_seconds: float = 1.5
    chatbot_reconcile_interval_seconds: float = 15.0
    chatbot_reconcile_limit: int = 50

    # --- Loop / lease / heartbeat ---
    worker_id: str = ""
    poll_interval_seconds: float = 1.0
    lease_seconds: int = 120
    heartbeat_interval_seconds: float = 5.0

    # --- Retention ---
    audio_retention_hours: int = 24
    retention_interval_seconds: float = 3600.0

    log_level: str = "INFO"

    @field_validator("transcription_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in MODE_ORDER:
            raise ValueError(f"TRANSCRIPTION_MODE must be one of {sorted(MODE_ORDER)}, got {v!r}")
        return v

    @field_validator("chatbot_mode")
    @classmethod
    def _valid_chatbot_mode(cls, v: str) -> str:
        v = (v or "disabled").strip().lower()
        if v not in CHATBOT_MODES:
            raise ValueError(f"CHATBOT_MODE must be one of {sorted(CHATBOT_MODES)}, got {v!r}")
        return v

    @property
    def resolved_worker_id(self) -> str:
        return self.worker_id or _default_worker_id()

    @property
    def chatbot_enabled(self) -> bool:
        return self.chatbot_mode != "disabled"

    @property
    def uses_agora(self) -> bool:
        return "agora" in MODE_ORDER[self.transcription_mode]

    @property
    def uses_faster_whisper(self) -> bool:
        return "faster_whisper" in MODE_ORDER[self.transcription_mode]

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def agora_configured(self) -> bool:
        """Credentials present (does NOT imply live is enabled)."""
        return bool(
            self.agora_app_id
            and self.agora_app_certificate
            and self.agora_customer_id
            and self.agora_customer_secret
        )


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
