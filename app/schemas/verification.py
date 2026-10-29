"""Schemas for external email verification."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VerificationResult(BaseModel):
    """Normalized result from an email verification provider."""

    email: str
    provider: str
    status: str
    reason: str | None = None
    raw: dict[str, Any] | None = None


class EmailVerificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    person_id: str
    email: str
    provider: str
    status: str
    reason: str | None = None
    checked_at: datetime


class PersonVerifyRequest(BaseModel):
    """Options for running external email verification."""

    person_ids: list[str] | None = None
    only_unverified: bool = Field(
        default=True,
        description="Skip people that already have an external verification row",
    )
    force: bool = False
    limit: int = Field(default=100, ge=1, le=1000)


class PersonVerifyResponse(BaseModel):
    company_id: str | None = None
    requested: int
    verified: int
    accepted: int
    rejected: int
    failed: int
    skipped: int
    required_external_status: str
    provider: str
    verifications: list[EmailVerificationRead] = Field(default_factory=list)
