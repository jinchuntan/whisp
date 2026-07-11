"""Question upload + polling endpoints.

POST is the async ingest: validate → store audio privately → insert a queued
row → 202 with a poll URL. No transcription happens here (Vercel-safe).
GET is the badge's poll endpoint.
"""

from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from persephone_api.auth import require_badge_key
from persephone_api.config import Settings, get_settings
from persephone_api.database import Database, get_database
from persephone_api.models import API_PREFIX, QuestionStatus
from persephone_api.schemas import QuestionAcceptedResponse, QuestionStatusResponse
from persephone_api.storage import Storage, get_storage
from persephone_api.wav import WavValidationError, parse_wav

log = logging.getLogger("persephone.questions")

router = APIRouter(tags=["questions"], dependencies=[Depends(require_badge_key)])

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_badge_id(badge_id: str) -> str:
    return _SANITIZE_RE.sub("_", badge_id)[:64]


def _validate_badge_id(badge_id: str, settings: Settings) -> None:
    if not badge_id or not re.match(settings.badge_id_pattern, badge_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or missing X-Badge-Id"
        )


@router.post(
    "/questions",
    response_model=QuestionAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_question(
    request: Request,
    response: Response,
    x_badge_id: str | None = Header(default=None),
    x_round_id: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    db: Database = Depends(get_database),
    storage: Storage = Depends(get_storage),
) -> QuestionAcceptedResponse:
    badge_id = (x_badge_id or "").strip()
    _validate_badge_id(badge_id, settings)

    # Reject oversized bodies early (before buffering) when Content-Length is set.
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > settings.max_audio_bytes
    ):
        raise HTTPException(
            status_code=413,
            detail=f"Audio exceeds {settings.max_audio_bytes} bytes",
        )

    body = await request.body()
    if len(body) > settings.max_audio_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Audio exceeds {settings.max_audio_bytes} bytes",
        )
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty body")

    try:
        parse_wav(body)
    except WavValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid WAV: {exc}"
        ) from exc

    # Resolve event/round context.
    event = await db.get_active_event()
    event_id = event["id"] if event else None

    round_id: str | None = None
    if x_round_id:
        rnd = await db.get_round(x_round_id.strip())
        if not rnd or rnd.get("status") != "open":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Round is not open")
        round_id = rnd["id"]
    elif event_id:
        open_round = await db.get_open_round(event_id)
        round_id = open_round["id"] if open_round else None

    await db.touch_badge(badge_id, event_id)

    # Store the audio privately under a generated UUID object name.
    object_path = f"{_sanitize_badge_id(badge_id)}/{uuid.uuid4()}.wav"
    try:
        await storage.upload_wav(object_path, body)
    except Exception:
        log.exception("audio upload failed for badge=%s", badge_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Failed to store audio"
        ) from None

    question = await db.create_question(
        event_id=event_id,
        round_id=round_id,
        badge_id=badge_id,
        audio_storage_path=object_path,
    )
    question_id = question["id"]
    log.info(
        "queued question id=%s badge=%s event=%s round=%s bytes=%d",
        question_id,
        badge_id,
        event_id,
        round_id,
        len(body),
    )

    poll_url = f"{API_PREFIX}/questions/{question_id}"
    response.headers["Location"] = poll_url
    return QuestionAcceptedResponse(
        question_id=question_id, status=QuestionStatus.QUEUED, poll_url=poll_url
    )


@router.get("/questions/{question_id}", response_model=QuestionStatusResponse)
async def get_question_status(
    question_id: str,
    db: Database = Depends(get_database),
) -> QuestionStatusResponse:
    question = await db.get_question(question_id)
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown question")

    st = question["status"]

    if st in (QuestionStatus.QUEUED.value, QuestionStatus.CLAIMED.value):
        return QuestionStatusResponse(question_id=question_id, status=QuestionStatus.QUEUED)

    if st == QuestionStatus.TRANSCRIBING.value:
        return QuestionStatusResponse(question_id=question_id, status=QuestionStatus.TRANSCRIBING)

    if st == QuestionStatus.DONE.value:
        similar_count = 1
        cluster_id = question.get("cluster_id")
        if cluster_id:
            cluster = await db.get_cluster(cluster_id)
            if cluster:
                similar_count = int(cluster.get("question_count") or 1)
        return QuestionStatusResponse(
            question_id=question_id,
            status=QuestionStatus.DONE,
            transcript=question.get("transcript") or "",
            provider=question.get("provider_used"),
            fallback_used=bool(question.get("fallback_used")),
            similar_count=similar_count,
            cluster_id=cluster_id,
        )

    if st == QuestionStatus.EMPTY.value:
        return QuestionStatusResponse(
            question_id=question_id,
            status=QuestionStatus.EMPTY,
            message="No speech detected",
        )

    # error (or any unexpected status) — never leak internals.
    return QuestionStatusResponse(
        question_id=question_id,
        status=QuestionStatus.ERROR,
        message=question.get("safe_error_message") or "Transcription unavailable",
    )
