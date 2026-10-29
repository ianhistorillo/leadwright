"""External email verification orchestration and final acceptance."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.enums import FinalStatus, VerificationStatus
from app.repositories.company_repository import CompanyRepository
from app.repositories.person_repository import PersonRepository
from app.repositories.verification_repository import VerificationRepository
from app.schemas.person import PersonRead
from app.schemas.verification import (
    EmailVerificationRead,
    PersonVerifyRequest,
    PersonVerifyResponse,
)
from app.services.acceptance import evaluate_final_acceptance
from app.services.email_verifier import (
    EmailVerifier,
    EmailVerifierError,
    create_email_verifier,
    safe_raw_response_json,
)
from app.utils.logging import get_logger

logger = get_logger(__name__)


class VerificationService:
    """Run external verification for Apollo-eligible contacts."""

    def __init__(
        self,
        session: Session,
        *,
        verifier: EmailVerifier | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._companies = CompanyRepository(session)
        self._people = PersonRepository(session)
        self._verifications = VerificationRepository(session)
        self._verifier = verifier

    async def verify_people(
        self,
        *,
        company_id: str | None = None,
        request: PersonVerifyRequest | None = None,
    ) -> PersonVerifyResponse:
        """
        Verify emails externally and set final_status.

        Only contacts that pass BOTH Apollo and external gates become `accepted`.
        """
        if not self._settings.email_verifier_enabled:
            raise ValueError(
                "External email verification is disabled "
                "(EMAIL_VERIFIER_ENABLED=false)"
            )

        options = request or PersonVerifyRequest()
        if company_id is not None and self._companies.get_by_id(company_id) is None:
            raise LookupError(f"Company not found: {company_id}")

        only_unverified = options.only_unverified and not options.force
        candidates = self._verifications.list_candidates(
            company_id=company_id,
            person_ids=options.person_ids,
            only_unverified=only_unverified,
            required_apollo_status=self._settings.apollo_required_email_status,
            limit=options.limit,
        )
        if options.person_ids and not candidates:
            raise LookupError(
                "No verifiable people found for the provided person_ids "
                "(need email + matching Apollo status)"
            )

        verifier = self._verifier or create_email_verifier(self._settings)
        owns_verifier = self._verifier is None

        verified = 0
        accepted = 0
        rejected = 0
        failed = 0
        skipped = 0
        stored: list[EmailVerificationRead] = []

        try:
            for person in candidates:
                if not person.email:
                    skipped += 1
                    continue

                logger.info(
                    "email_verification_request",
                    extra={
                        "person_id": person.id,
                        "company_id": person.company_id,
                        "provider": verifier.provider_name,
                    },
                )
                try:
                    result = await verifier.verify(person.email)
                except EmailVerifierError as exc:
                    failed += 1
                    row = self._verifications.create(
                        person_id=person.id,
                        email=person.email,
                        provider=verifier.provider_name,
                        status=VerificationStatus.FAILED.value,
                        reason=str(exc)[:500],
                        raw_response=None,
                    )
                    person.final_status = FinalStatus.FAILED.value
                    person.rejection_reason = f"external_verification_failed: {exc}"[:500]
                    self._session.flush()
                    stored.append(EmailVerificationRead.model_validate(row))
                    continue

                verified += 1
                row = self._verifications.create(
                    person_id=person.id,
                    email=result.email,
                    provider=result.provider,
                    status=result.status,
                    reason=result.reason,
                    raw_response=safe_raw_response_json(result.raw),
                )

                decision = evaluate_final_acceptance(
                    apollo_email_status=person.apollo_email_status,
                    external_status=result.status,
                    settings=self._settings,
                )
                if decision.accepted:
                    person.final_status = FinalStatus.ACCEPTED.value
                    person.rejection_reason = None
                    accepted += 1
                else:
                    # Map common external statuses onto final_status when useful.
                    if result.status == VerificationStatus.INVALID.value:
                        person.final_status = FinalStatus.INVALID.value
                    elif result.status == VerificationStatus.RISKY.value:
                        person.final_status = FinalStatus.RISKY.value
                    elif result.status == VerificationStatus.DISPOSABLE.value:
                        person.final_status = FinalStatus.DISPOSABLE.value
                    elif result.status == VerificationStatus.CATCH_ALL.value:
                        person.final_status = FinalStatus.CATCH_ALL.value
                    elif result.status == VerificationStatus.UNKNOWN.value:
                        person.final_status = FinalStatus.UNKNOWN.value
                    else:
                        person.final_status = FinalStatus.REJECTED.value
                    person.rejection_reason = decision.reason
                    rejected += 1

                self._session.flush()
                stored.append(EmailVerificationRead.model_validate(row))

            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        finally:
            if owns_verifier:
                await verifier.aclose()

        return PersonVerifyResponse(
            company_id=company_id,
            requested=len(candidates),
            verified=verified,
            accepted=accepted,
            rejected=rejected,
            failed=failed,
            skipped=skipped,
            required_external_status=self._settings.external_required_email_status,
            provider=verifier.provider_name,
            verifications=stored,
        )

    def list_accepted_people(
        self,
        *,
        company_id: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> list[PersonRead]:
        people = self._people.list_people(offset=0, limit=10_000)
        filtered = [
            p
            for p in people
            if p.final_status == FinalStatus.ACCEPTED.value
            and (company_id is None or p.company_id == company_id)
        ]
        page = filtered[offset : offset + limit]
        return [PersonRead.model_validate(p) for p in page]
