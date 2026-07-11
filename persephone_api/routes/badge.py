"""Badge-facing endpoints (require X-Persephone-Key)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from persephone_api.auth import require_badge_key
from persephone_api.config import Settings, get_settings
from persephone_api.database import Database, get_database
from persephone_api.schemas import BadgeNotification, BadgeStateResponse

router = APIRouter(tags=["badge"], dependencies=[Depends(require_badge_key)])


def _validate_badge_id(badge_id: str, settings: Settings) -> None:
    if not re.match(settings.badge_id_pattern, badge_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid badge_id format",
        )


@router.get("/badge/state", response_model=BadgeStateResponse)
async def badge_state(
    badge_id: str = Query(..., min_length=1, max_length=64),
    db: Database = Depends(get_database),
    settings: Settings = Depends(get_settings),
) -> BadgeStateResponse:
    _validate_badge_id(badge_id, settings)

    event = await db.get_active_event()
    event_id = event["id"] if event else None
    open_round = await db.get_open_round(event_id) if event_id else None

    await db.touch_badge(badge_id, event_id)

    notifications: list[BadgeNotification] = []
    if event_id:
        round_id = open_round["id"] if open_round else None
        for note in await db.get_badge_notifications(badge_id, round_id):
            notifications.append(BadgeNotification(**note))

    return BadgeStateResponse(
        event_id=event_id,
        event_name=event["name"] if event else None,
        round_id=open_round["id"] if open_round else None,
        round_prompt=open_round["prompt"] if open_round else None,
        accepting=event is not None,
        notifications=notifications,
        server_time=datetime.now(timezone.utc),
    )
