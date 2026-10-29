"""Pydantic schemas for people search and listing."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonRead(BaseModel):
    """Person representation returned by the API."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    apollo_person_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    job_title: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    apollo_email_status: str | None = None
    final_status: str
    rejection_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class PersonListResponse(BaseModel):
    total: int
    items: list[PersonRead]


class PersonSearchRequest(BaseModel):
    """Filters for searching employees at a company via Apollo."""

    job_title_profile: str | None = Field(
        default=None,
        description="Named profile: executive, sales, marketing, technology, finance, custom",
    )
    job_titles: list[str] | None = Field(
        default=None,
        description="Explicit title override; wins over profile/settings",
    )
    person_seniorities: list[str] | None = Field(
        default=None,
        description="Apollo seniorities: owner, founder, c_suite, partner, vp, head, ...",
    )
    person_locations: list[str] | None = Field(
        default=None,
        description="Personal locations supported by Apollo People API Search",
    )
    organization_locations: list[str] | None = None
    contact_email_status: list[
        Literal["verified", "unverified", "likely to engage", "unavailable"]
    ] | None = Field(
        default=None,
        description="Apollo contact_email_status filter values",
    )
    include_similar_titles: bool = True
    per_page: int = Field(default=25, ge=1, le=100)
    max_pages: int = Field(
        default=5,
        ge=1,
        le=1000,
        description="Safety cap on Apollo search pagination",
    )
    skip_existing_people: bool = Field(
        default=False,
        description=(
            "When true, skip Apollo hits already stored in the DB "
            "(matched by apollo_person_id for that company)."
        ),
    )
    source_job_id: str | None = Field(
        default=None,
        description="Optional job id stamped on newly created people for this run.",
    )


class PersonSearchResponse(BaseModel):
    company_id: str
    company_domain: str
    job_titles_used: list[str]
    apollo_total_entries: int | None = None
    pages_fetched: int
    discovered: int
    created: int
    updated: int
    skipped_unchanged: int
    skipped_existing: int = 0
    people: list[PersonRead] = Field(default_factory=list)


class PersonEnrichRequest(BaseModel):
    """Options for Apollo people enrichment."""

    person_ids: list[str] | None = Field(
        default=None,
        description="Optional subset of person IDs; defaults to company (or all) candidates",
    )
    only_missing_email: bool = Field(
        default=True,
        description="When true, skip people that already have an email unless force=true",
    )
    force: bool = Field(
        default=False,
        description="Re-enrich even when an email is already stored",
    )
    source_job_id: str | None = Field(
        default=None,
        description="When set, only enrich people created by this job.",
    )
    limit: int = Field(default=100, ge=1, le=1000)


class PersonEnrichResponse(BaseModel):
    company_id: str | None = None
    requested: int
    enriched: int
    apollo_accepted: int
    apollo_rejected: int
    missing_matches: int
    batches: int
    required_apollo_email_status: str
    people: list[PersonRead] = Field(default_factory=list)
