"""People search, enrichment, and listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.database.session import get_db
from app.schemas.person import (
    PersonEnrichRequest,
    PersonEnrichResponse,
    PersonListResponse,
    PersonRead,
    PersonSearchRequest,
    PersonSearchResponse,
)
from app.schemas.verification import PersonVerifyRequest, PersonVerifyResponse
from app.services.apollo import ApolloAuthError, ApolloError, ApolloValidationError
from app.services.email_verifier import EmailVerifierError
from app.services.job_title_profiles import list_job_title_profiles
from app.services.people_service import PeopleService
from app.services.verification_service import VerificationService

router = APIRouter(tags=["people"], dependencies=[Depends(require_api_key)])


def _map_people_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, (ValueError, ApolloValidationError)):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, (ApolloAuthError, EmailVerifierError, ApolloError)):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected people service error",
    )


@router.get("/people/job-title-profiles")
def get_job_title_profiles() -> dict[str, list[str]]:
    """List available job-title profile names."""
    return {"profiles": list_job_title_profiles()}


@router.get("/people", response_model=PersonListResponse)
def list_people(
    company_id: str | None = Query(default=None),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> PersonListResponse:
    """List discovered people, optionally filtered by company."""
    return PeopleService(db).list_people(
        company_id=company_id,
        offset=offset,
        limit=limit,
    )


@router.get("/people/{person_id}", response_model=PersonRead)
def get_person(person_id: str, db: Session = Depends(get_db)) -> PersonRead:
    """Fetch a single person by ID."""
    person = PeopleService(db).get_person(person_id)
    if person is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Person not found",
        )
    return PersonRead.model_validate(person)


@router.post(
    "/companies/{company_id}/people/search",
    response_model=PersonSearchResponse,
)
async def search_company_people(
    company_id: str,
    payload: PersonSearchRequest | None = None,
    db: Session = Depends(get_db),
) -> PersonSearchResponse:
    """Search Apollo for employees at a company domain and store person metadata."""
    try:
        return await PeopleService(db).search_company_people(company_id, payload)
    except Exception as exc:
        mapped = _map_people_errors(exc)
        if mapped.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise mapped from exc


@router.post(
    "/companies/{company_id}/people/enrich",
    response_model=PersonEnrichResponse,
)
async def enrich_company_people(
    company_id: str,
    payload: PersonEnrichRequest | None = None,
    db: Session = Depends(get_db),
) -> PersonEnrichResponse:
    """
    Enrich discovered people for a company via Apollo bulk_match.

    Applies APOLLO_REQUIRED_EMAIL_STATUS. Does not run external verification.
    """
    try:
        return await PeopleService(db).enrich_people(
            company_id=company_id,
            request=payload,
        )
    except Exception as exc:
        mapped = _map_people_errors(exc)
        if mapped.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise mapped from exc


@router.post(
    "/people/enrich",
    response_model=PersonEnrichResponse,
)
async def enrich_people(
    payload: PersonEnrichRequest,
    db: Session = Depends(get_db),
) -> PersonEnrichResponse:
    """Enrich specific people by ID (or candidates when person_ids omitted)."""
    try:
        return await PeopleService(db).enrich_people(request=payload)
    except Exception as exc:
        mapped = _map_people_errors(exc)
        if mapped.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise mapped from exc


@router.post(
    "/companies/{company_id}/people/verify",
    response_model=PersonVerifyResponse,
)
async def verify_company_people(
    company_id: str,
    payload: PersonVerifyRequest | None = None,
    db: Session = Depends(get_db),
) -> PersonVerifyResponse:
    """
    Run external email verification for Apollo-eligible people at a company.

    final_status becomes accepted only when Apollo AND external gates both pass.
    """
    try:
        return await VerificationService(db).verify_people(
            company_id=company_id,
            request=payload,
        )
    except Exception as exc:
        mapped = _map_people_errors(exc)
        if mapped.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise mapped from exc


@router.post(
    "/people/verify",
    response_model=PersonVerifyResponse,
)
async def verify_people(
    payload: PersonVerifyRequest | None = None,
    db: Session = Depends(get_db),
) -> PersonVerifyResponse:
    """Run external email verification for eligible people."""
    try:
        return await VerificationService(db).verify_people(request=payload)
    except Exception as exc:
        mapped = _map_people_errors(exc)
        if mapped.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            raise
        raise mapped from exc
