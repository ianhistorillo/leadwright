"""Email verification persistence helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.database.models import EmailVerification, Person


class VerificationRepository:
    """Data-access layer for EmailVerification records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        *,
        person_id: str,
        email: str,
        provider: str,
        status: str,
        reason: str | None = None,
        raw_response: str | None = None,
        checked_at: datetime | None = None,
    ) -> EmailVerification:
        row = EmailVerification(
            person_id=person_id,
            email=email,
            provider=provider,
            status=status,
            reason=reason,
            raw_response=raw_response,
            checked_at=checked_at or datetime.now(timezone.utc),
        )
        self._session.add(row)
        return row

    def has_verification(self, person_id: str) -> bool:
        stmt = select(
            exists().where(EmailVerification.person_id == person_id)
        )
        return bool(self._session.scalar(stmt))

    def latest_for_person(self, person_id: str) -> EmailVerification | None:
        stmt = (
            select(EmailVerification)
            .where(EmailVerification.person_id == person_id)
            .order_by(EmailVerification.checked_at.desc())
            .limit(1)
        )
        return self._session.scalar(stmt)

    def list_candidates(
        self,
        *,
        company_id: str | None = None,
        person_ids: list[str] | None = None,
        only_unverified: bool = True,
        required_apollo_status: str,
        limit: int = 1000,
    ) -> list[Person]:
        """
        People eligible for external verification:

        - have an email
        - Apollo email status matches the required Apollo status
        - optionally no prior external verification row
        """
        stmt = select(Person).where(
            Person.email.is_not(None),
            Person.email != "",
            Person.apollo_email_status.is_not(None),
        )
        # Case-insensitive compare via lower() on both sides is dialect-dependent;
        # store/compare normalized lowercase values in app layer filters below.
        if company_id:
            stmt = stmt.where(Person.company_id == company_id)
        if person_ids:
            stmt = stmt.where(Person.id.in_(person_ids))
        if only_unverified:
            stmt = stmt.where(
                ~exists().where(EmailVerification.person_id == Person.id)
            )
        stmt = stmt.order_by(Person.created_at.asc()).limit(limit * 3)
        rows = list(self._session.scalars(stmt).all())
        required = required_apollo_status.strip().lower()
        filtered = [
            person
            for person in rows
            if (person.apollo_email_status or "").strip().lower() == required
        ]
        return filtered[:limit]
