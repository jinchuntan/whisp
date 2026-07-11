"""Worker configuration (env / .env). Never contacts any service on import."""

from __future__ import annotations

import os
import socket
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from whisp_worker.providers.router import MODE_ORDER


def _default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Supabase ---
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_audio_bucket: str = "whisp-audio"

    # --- Transcription mode (authoritative selector) ---
    transcription_mode: str = "faster_whisper_only"

    # --- Faster-Whisper ---
    faster_whisper_model: str = "base"
    faster_whisper_device: str = "cpu"
    faster_whisper_compute_type: str = "int8"
    faster_whisper_beam_size: int = 5
    transcription_language: str = "en"

    # --- Agora (only used when the mode selects Agora) ---
    agora_app_id: str = ""
    agora_app_certificate: str = ""
    agora_customer_id: str = ""
    agora_customer_secret: str = ""
    agora_max_duration_seconds: int = 30
    agora_idle_seconds: int = 10

    # --- Provider timeouts (seconds) ---
    faster_whisper_timeout: float = 120.0
    agora_timeout: float = 60.0

    # --- Clustering ---
    enable_clustering: bool = True
    cluster_similarity_threshold: float = 0.78
    embedding_model: str = "all-MiniLM-L6-v2"

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

    @property
    def resolved_worker_id(self) -> str:
        return self.worker_id or _default_worker_id()

    @property
    def uses_agora(self) -> bool:
        return "agora" in MODE_ORDER[self.transcription_mode]

    @property
    def uses_faster_whisper(self) -> bool:
        return "faster_whisper" in MODE_ORDER[self.transcription_mode]

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)


@lru_cache
def get_worker_settings() -> WorkerSettings:
    return WorkerSettings()
