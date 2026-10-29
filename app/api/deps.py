"""Optional API key authentication for public exposure."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Enforce API key auth when API_KEY is configured.

    When API_KEY is empty/unset, requests are allowed (local development).
    """
    settings = get_settings()
    expected = (settings.api_key or "").strip()
    if not expected:
        return
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
