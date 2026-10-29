"""Pydantic schemas for processing jobs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.person import PersonRead


class JobCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    company_ids: list[str] | None = Field(
        default=None,
        description="Optional company IDs; defaults to all imported companies",
    )
    job_title_profile: str | None = None
    job_titles: list[str] | None = None
    contact_email_status: list[str] | None = None
    max_pages: int = Field(default=2, ge=1, le=1000)
    run_external_verification: bool = False
    pipeline_mode: str = Field(
        default="search_and_enrich",
        description=(
            "search_only_guess = Apollo search + local email guesses (0 enrich credits); "
            "search_and_enrich = search + Apollo email enrichment (uses credits); "
            "ZeroBounce is off unless EMAIL_VERIFIER_ENABLED=true and verify is requested"
        ),
    )
    skip_existing_people: bool = Field(
        default=False,
        description=(
            "When true, only keep Apollo people not already in the DB for this batch "
            "(export/results exclude previously discovered contacts)."
        ),
    )


class JobRunOptions(BaseModel):
    """Options accepted by start/resume/retry-failed."""

    job_title_profile: str | None = None
    job_titles: list[str] | None = None
    contact_email_status: list[str] | None = None
    max_pages: int = Field(default=2, ge=1, le=1000)
    per_page: int = Field(default=25, ge=1, le=100)
    batch_size: int | None = Field(default=None, ge=1, le=1000)
    run_external_verification: bool = False
    pipeline_mode: str = "search_only_guess"
    skip_existing_people: bool = False


class JobItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str
    company_id: str
    status: str
    retry_count: int
    error_message: str | None = None
    processed_at: datetime | None = None


class ProcessingErrorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str | None = None
    job_item_id: str | None = None
    company_id: str | None = None
    provider: str | None = None
    operation: str
    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool
    attempt: int
    created_at: datetime


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    status: str
    total_companies: int
    processed_companies: int
    successful_companies: int
    failed_companies: int
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class JobListResponse(BaseModel):
    total: int
    items: list[JobRead]


class JobProgressResponse(BaseModel):
    job: JobRead
    pending: int
    searching: int
    enriching: int
    verifying: int
    completed: int
    failed: int
    percent_complete: float


class JobResultsResponse(BaseModel):
    job_id: str
    accepted_people: list[PersonRead]
    guessed_people: list[PersonRead] = Field(default_factory=list)
    rejected_people: list[PersonRead]
    failed_items: list[JobItemRead]


class ProcessingErrorRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    job_id: str | None = None
    job_item_id: str | None = None
    company_id: str | None = None
    provider: str | None = None
    operation: str
    error_type: str
    message: str
    http_status: int | None = None
    retryable: bool
    attempt: int
    created_at: datetime
