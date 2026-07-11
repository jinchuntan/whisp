"""Pydantic request/response schemas for the Whisp API.

These define the wire contract documented in docs/API.md. Responses are built
explicitly (never dumping raw DB rows) so internal columns and secrets are never
leaked to badges or browsers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from whisp_api.models import QuestionStatus


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str = "ok"
    service: str = "whisp-api"
    version: str
    time: datetime
    supabase_configured: bool


# ---------------------------------------------------------------------------
# Badge
# ---------------------------------------------------------------------------
class BadgeNotification(BaseModel):
    question_id: str
    cluster_id: str | None = None
    similar_count: int = 1
    canonical_question: str | None = None


class BadgeStateResponse(BaseModel):
    event_id: str | None = None
    event_name: str | None = None
    round_id: str | None = None
    round_prompt: str | None = None
    accepting: bool = False
    notifications: list[BadgeNotification] = Field(default_factory=list)
    server_time: datetime


class QuestionAcceptedResponse(BaseModel):
    ok: bool = True
    question_id: str
    status: QuestionStatus = QuestionStatus.QUEUED
    poll_url: str


class QuestionStatusResponse(BaseModel):
    """Polling response. Fields beyond status appear only when relevant."""

    question_id: str
    status: QuestionStatus
    transcript: str | None = None
    provider: str | None = None
    fallback_used: bool | None = None
    similar_count: int | None = None
    cluster_id: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Admin — requests
# ---------------------------------------------------------------------------
class CreateEventRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class CreateRoundRequest(BaseModel):
    prompt: str | None = Field(default=None, max_length=500)
    event_id: str | None = None


# ---------------------------------------------------------------------------
# Admin — responses
# ---------------------------------------------------------------------------
class EventOut(BaseModel):
    id: str
    name: str
    join_code: str
    active: bool
    created_at: datetime | None = None


class RoundOut(BaseModel):
    id: str
    event_id: str
    prompt: str | None = None
    status: str
    opened_at: datetime | None = None
    closed_at: datetime | None = None


class QuestionOut(BaseModel):
    id: str
    status: str
    badge_id: str | None = None
    transcript: str | None = None
    provider_used: str | None = None
    fallback_used: bool = False
    processing_ms: int | None = None
    language: str | None = None
    round_id: str | None = None
    cluster_id: str | None = None
    similar_count: int | None = None
    safe_error_message: str | None = None
    created_at: datetime | None = None
    answered_at: datetime | None = None


class ClusterOut(BaseModel):
    id: str
    round_id: str | None = None
    canonical_question: str
    question_count: int
    status: str
    created_at: datetime | None = None
    answered_at: datetime | None = None


class WorkerHealthOut(BaseModel):
    worker_id: str
    version: str | None = None
    transcription_mode: str | None = None
    status: str | None = None
    last_seen_at: datetime | None = None
    online: bool = False


class AdminStateResponse(BaseModel):
    server_time: datetime
    transcription_mode: str
    agora_mode_active: bool
    event: EventOut | None = None
    open_round: RoundOut | None = None
    rounds: list[RoundOut] = Field(default_factory=list)
    questions: list[QuestionOut] = Field(default_factory=list)
    clusters: list[ClusterOut] = Field(default_factory=list)
    workers: list[WorkerHealthOut] = Field(default_factory=list)
    worker_online: bool = False


class WorkerHealthResponse(BaseModel):
    server_time: datetime
    worker_online: bool
    workers: list[WorkerHealthOut] = Field(default_factory=list)


class OkResponse(BaseModel):
    ok: bool = True


class ErrorResponse(BaseModel):
    ok: bool = False
    error: str
    message: str
