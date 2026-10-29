"""Health check endpoints."""

from datetime import datetime, timezone

from fastapi import APIRouter

from app import __version__
from app.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Return service liveness information."""
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": __version__,
        "environment": settings.app_env,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
