"""Admin/host endpoints (require Authorization: Bearer <ADMIN_API_KEY>)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from persephone_api.auth import require_admin_user
from persephone_api.config import Settings, get_settings
from persephone_api.database import Database, get_database
from persephone_api.models import AGORA_MODES
from persephone_api.schemas import (
    AdminStateResponse,
    ClusterOut,
    CreateEventRequest,
    CreateRoundRequest,
    EventOut,
    OkResponse,
    QuestionOut,
    RoundOut,
    WorkerHealthOut,
    WorkerHealthResponse,
)

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin_user)])

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _join_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(6))


def _event_out(row: dict[str, Any]) -> EventOut:
    return EventOut(
        id=row["id"],
        name=row["name"],
        join_code=row["join_code"],
        active=bool(row.get("active")),
        created_at=_parse_ts(row.get("created_at")),
    )


def _round_out(row: dict[str, Any]) -> RoundOut:
    return RoundOut(
        id=row["id"],
        event_id=row["event_id"],
        prompt=row.get("prompt"),
        status=row["status"],
        opened_at=_parse_ts(row.get("opened_at")),
        closed_at=_parse_ts(row.get("closed_at")),
    )


def _question_out(row: dict[str, Any], cluster_counts: dict[str, int]) -> QuestionOut:
    cluster_id = row.get("cluster_id")
    similar = cluster_counts.get(cluster_id) if cluster_id else None
    return QuestionOut(
        id=row["id"],
        status=row["status"],
        badge_id=row.get("badge_id"),
        transcript=row.get("transcript"),
        provider_used=row.get("provider_used"),
        fallback_used=bool(row.get("fallback_used")),
        processing_ms=row.get("processing_ms"),
        language=row.get("language"),
        round_id=row.get("round_id"),
        cluster_id=cluster_id,
        similar_count=similar,
        safe_error_message=row.get("safe_error_message"),
        created_at=_parse_ts(row.get("created_at")),
        answered_at=_parse_ts(row.get("answered_at")),
    )


def _cluster_out(row: dict[str, Any]) -> ClusterOut:
    return ClusterOut(
        id=row["id"],
        round_id=row.get("round_id"),
        canonical_question=row["canonical_question"],
        question_count=int(row.get("question_count") or 0),
        status=row.get("status") or "open",
        created_at=_parse_ts(row.get("created_at")),
        answered_at=_parse_ts(row.get("answered_at")),
    )


def _worker_out(row: dict[str, Any], offline_seconds: int, now: datetime) -> WorkerHealthOut:
    last_seen = _parse_ts(row.get("last_seen_at"))
    online = bool(last_seen and (now - last_seen).total_seconds() <= offline_seconds)
    return WorkerHealthOut(
        worker_id=row["worker_id"],
        version=row.get("version"),
        transcription_mode=row.get("transcription_mode"),
        status=row.get("status"),
        last_seen_at=last_seen,
        online=online,
    )


@router.get("/state", response_model=AdminStateResponse)
async def admin_state(
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
) -> AdminStateResponse:
    now = _now()
    event = await db.get_active_event()
    event_id = event["id"] if event else None

    rounds_rows = await db.list_rounds(event_id) if event_id else []
    open_round_row = await db.get_open_round(event_id) if event_id else None

    clusters_rows: list[dict[str, Any]] = []
    if open_round_row:
        clusters_rows = await db.list_clusters(open_round_row["id"])
    cluster_counts = {c["id"]: int(c.get("question_count") or 0) for c in clusters_rows}

    questions_rows = await db.list_recent_questions(event_id)
    workers_rows = await db.list_worker_heartbeats()
    workers = [_worker_out(w, settings.worker_offline_seconds, now) for w in workers_rows]

    return AdminStateResponse(
        server_time=now,
        transcription_mode=settings.transcription_mode.value,
        agora_mode_active=settings.transcription_mode in AGORA_MODES,
        event=_event_out(event) if event else None,
        open_round=_round_out(open_round_row) if open_round_row else None,
        rounds=[_round_out(r) for r in rounds_rows],
        questions=[_question_out(q, cluster_counts) for q in questions_rows],
        clusters=[_cluster_out(c) for c in clusters_rows],
        workers=workers,
        worker_online=any(w.online for w in workers),
    )


@router.post("/events", response_model=EventOut, status_code=status.HTTP_201_CREATED)
async def create_event(body: CreateEventRequest, db: Database = Depends(get_database)) -> EventOut:
    event = await db.create_event(body.name.strip(), _join_code())
    await db.deactivate_events_except(event["id"])
    return _event_out(event)


@router.post("/rounds", response_model=RoundOut, status_code=status.HTTP_201_CREATED)
async def create_round(body: CreateRoundRequest, db: Database = Depends(get_database)) -> RoundOut:
    event_id = body.event_id
    if not event_id:
        event = await db.get_active_event()
        if not event:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No active event; create one first",
            )
        event_id = event["id"]
    # Only one open round per event.
    await db.close_open_rounds(event_id)
    rnd = await db.create_round(event_id, (body.prompt or None))
    return _round_out(rnd)


@router.post("/rounds/{round_id}/close", response_model=RoundOut)
async def close_round(round_id: str, db: Database = Depends(get_database)) -> RoundOut:
    rnd = await db.close_round(round_id)
    if not rnd:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown round")
    return _round_out(rnd)


@router.post("/questions/{question_id}/answered", response_model=OkResponse)
async def mark_question_answered(
    question_id: str, db: Database = Depends(get_database)
) -> OkResponse:
    if not await db.mark_question_answered(question_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown question")
    return OkResponse()


@router.post("/clusters/{cluster_id}/answered", response_model=OkResponse)
async def mark_cluster_answered(
    cluster_id: str, db: Database = Depends(get_database)
) -> OkResponse:
    if not await db.mark_cluster_answered(cluster_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown cluster")
    return OkResponse()


@router.post("/recluster", response_model=OkResponse)
async def recluster(
    round_id: str | None = None, db: Database = Depends(get_database)
) -> OkResponse:
    target = round_id
    if not target:
        event = await db.get_active_event()
        open_round = await db.get_open_round(event["id"]) if event else None
        target = open_round["id"] if open_round else None
    if not target:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No round to recluster")
    await db.request_recluster(target)
    return OkResponse()


@router.get("/worker-health", response_model=WorkerHealthResponse)
async def worker_health(
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
) -> WorkerHealthResponse:
    now = _now()
    workers = [
        _worker_out(w, settings.worker_offline_seconds, now)
        for w in await db.list_worker_heartbeats()
    ]
    return WorkerHealthResponse(
        server_time=now,
        worker_online=any(w.online for w in workers),
        workers=workers,
    )
