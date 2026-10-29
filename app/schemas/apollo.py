"""Pydantic models for Apollo official API requests/responses.

Source of truth:
- https://docs.apollo.io/reference/people-api-search
- https://docs.apollo.io/reference/people-enrichment
- https://docs.apollo.io/reference/bulk-people-enrichment
- https://docs.apollo.io/reference/authentication
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, AliasChoices, model_validator


class ApolloModel(BaseModel):
    """Base model that ignores undocumented Apollo response fields."""

    model_config = ConfigDict(extra="ignore")


class ApolloOrganizationSummary(ApolloModel):
    id: str | None = None
    name: str | None = None
    primary_domain: str | None = None
    website_url: str | None = None
    linkedin_url: str | None = None


class ApolloPersonSearchResult(ApolloModel):
    """Person record returned by People API Search (no emails/phones)."""

    id: str
    first_name: str | None = None
    last_name_obfuscated: str | None = None
    last_name: str | None = None
    name: str | None = None
    title: str | None = None
    has_email: bool | None = None
    has_city: bool | None = None
    has_state: bool | None = None
    has_country: bool | None = None
    has_direct_phone: str | None = None
    # Official Search docs usually omit this; capture it whenever Apollo includes it
    # (0 credits). Enrichment is still the documented path for LinkedIn.
    linkedin_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "linkedin_url",
            "linkedin",
            "person_linkedin_url",
        ),
    )
    organization: ApolloOrganizationSummary | None = None

    @model_validator(mode="before")
    @classmethod
    def _pull_nested_linkedin(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("linkedin_url") or data.get("linkedin"):
            return data
        # Rare nested shapes some Apollo payloads use.
        for key in ("contact", "person"):
            nested = data.get(key)
            if isinstance(nested, dict):
                url = nested.get("linkedin_url") or nested.get("linkedin")
                if url:
                    data = {**data, "linkedin_url": url}
                    break
        return data


class ApolloPeopleSearchResponse(ApolloModel):
    total_entries: int | None = None
    # Apollo may return null slots (same pattern as bulk_match).
    people: list[ApolloPersonSearchResult | None] = Field(default_factory=list)
    pagination: dict[str, Any] | None = None


class ApolloPeopleSearchRequest(ApolloModel):
    """
    Request body for POST /mixed_people/api_search.

    Parameter names match Apollo docs. Arrays may be sent in JSON body
    (documented alternative to query-string `param[]` form).
    """

    person_titles: list[str] | None = None
    include_similar_titles: bool | None = True
    person_locations: list[str] | None = None
    person_seniorities: list[str] | None = None
    organization_locations: list[str] | None = None
    q_organization_domains_list: list[str] | None = None
    # Not always listed in current People API Search docs; widely used and free.
    # Used when CSV has company_name but no website/domain.
    q_organization_name: str | None = None
    contact_email_status: list[str] | None = None
    organization_ids: list[str] | None = None
    page: int | None = 1
    per_page: int | None = 25
    q_keywords: str | None = None


class ApolloEnrichPersonRequest(ApolloModel):
    """Single-person enrichment identifiers for POST /people/match."""

    id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    email: str | None = None
    hashed_email: str | None = None
    organization_name: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None
    reveal_personal_emails: bool = False
    reveal_phone_number: bool = False
    run_waterfall_email: bool = False
    run_waterfall_phone: bool = False
    webhook_url: str | None = None


class ApolloBulkEnrichPersonDetail(ApolloModel):
    """One entry in bulk enrichment `details` (max 10 per request)."""

    id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    email: str | None = None
    hashed_email: str | None = None
    organization_name: str | None = None
    domain: str | None = None
    linkedin_url: str | None = None


class ApolloEnrichedPerson(ApolloModel):
    """Person record returned by enrichment endpoints (may include email)."""

    id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    name: str | None = None
    title: str | None = None
    linkedin_url: str | None = None
    email: str | None = None
    email_status: str | None = None
    organization_id: str | None = None
    organization: ApolloOrganizationSummary | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None


class ApolloPeopleMatchResponse(ApolloModel):
    person: ApolloEnrichedPerson | None = None


class ApolloBulkPeopleMatchResponse(ApolloModel):
    status: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    total_requested_enrichments: int | None = None
    unique_enriched_records: int | None = None
    missing_records: int | None = None
    credits_consumed: int | float | None = None
    # Apollo returns null for unmatched slots (aligned with request order).
    matches: list[ApolloEnrichedPerson | None] = Field(default_factory=list)
