"""Pydantic schemas for company import and listing."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CompanyRead(BaseModel):
    """Company representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str | None = None
    lookup_name: str | None = None
    website: str | None = None
    domain: str
    apollo_id: str | None = None
    status: str
    created_at: datetime
    updated_at: datetime


class InvalidImportRow(BaseModel):
    """Details for a CSV row that failed validation."""

    row_number: int = Field(description="1-based data row number (excluding header)")
    value: str | None = None
    reason: str


class CompanyImportSummary(BaseModel):
    """Summary returned after a CSV import."""

    total_rows: int
    valid_companies: int
    invalid_rows: int
    duplicate_domains: int
    existing_domains_skipped: int = 0
    companies: list[CompanyRead] = Field(default_factory=list)
    invalid_details: list[InvalidImportRow] = Field(default_factory=list)


class CompanyListResponse(BaseModel):
    """Paginated company list response."""

    total: int
    items: list[CompanyRead]
