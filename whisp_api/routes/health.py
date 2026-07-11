"""Health endpoint. Reports API status WITHOUT touching Supabase or Agora."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from whisp_api import __version__
from whisp_api.config import Settings, get_settings
from whisp_api.schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(
        version=__version__,
        time=datetime.now(timezone.utc),
        supabase_configured=settings.supabase_configured,
    )
