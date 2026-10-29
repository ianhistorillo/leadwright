"""Person persistence helpers."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.enums import FinalStatus
from app.database.models import Person


class PersonRepository:
    """Data-access layer for Person records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, person_id: str) -> Person | None:
        return self._session.get(Person, person_id)

    def get_by_apollo_person_id(self, apollo_person_id: str) -> Person | None:
        """
        Find a person by Apollo ID, including unflushed session objects.

        The app session uses autoflush=False, so a plain SELECT would miss
        people added earlier in the same search batch and cause UNIQUE errors.
        """
        if not apollo_person_id:
            return None
        for obj in list(self._session.new) + list(self._session.dirty):
            if isinstance(obj, Person) and obj.apollo_person_id == apollo_person_id:
                return obj
        for obj in self._session.identity_map.values():
            if isinstance(obj, Person) and obj.apollo_person_id == apollo_person_id:
                return obj
        stmt = select(Person).where(Person.apollo_person_id == apollo_person_id)
        return self._session.scalar(stmt)

    def list_by_company(
        self,
        company_id: str,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> list[Person]:
        stmt = (
            select(Person)
            .where(Person.company_id == company_id)
            .order_by(Person.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def count_by_company(self, company_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(Person)
            .where(Person.company_id == company_id)
        )
        return int(self._session.scalar(stmt) or 0)

    def list_people(self, *, offset: int = 0, limit: int = 100) -> list[Person]:
        stmt = (
            select(Person)
            .order_by(Person.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def list_for_enrichment(
        self,
        *,
        company_id: str | None = None,
        person_ids: list[str] | None = None,
        only_missing_email: bool = True,
        source_job_id: str | None = None,
        limit: int = 1000,
    ) -> list[Person]:
        """Return people eligible for Apollo enrichment."""
        stmt = select(Person).where(Person.apollo_person_id.is_not(None))
        if company_id:
            stmt = stmt.where(Person.company_id == company_id)
        if person_ids:
            stmt = stmt.where(Person.id.in_(person_ids))
        if source_job_id:
            stmt = stmt.where(Person.source_job_id == source_job_id)
        if only_missing_email:
            stmt = stmt.where((Person.email.is_(None)) | (Person.email == ""))
        stmt = stmt.order_by(Person.created_at.asc()).limit(limit)
        return list(self._session.scalars(stmt).all())

    def get_by_normalized_email(self, normalized_email: str) -> Person | None:
        """
        Find by normalized email, including unflushed session objects.

        Needed when assigning many guessed emails in one commit (autoflush off).
        """
        if not normalized_email:
            return None
        for obj in self._session.new:
            if (
                isinstance(obj, Person)
                and obj.normalized_email == normalized_email
            ):
                return obj
        for obj in self._session.dirty:
            if (
                isinstance(obj, Person)
                and obj.normalized_email == normalized_email
            ):
                return obj
        stmt = select(Person).where(Person.normalized_email == normalized_email)
        return self._session.scalar(stmt)

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(Person)) or 0)

    def create(
        self,
        *,
        company_id: str,
        apollo_person_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        full_name: str | None = None,
        job_title: str | None = None,
        linkedin_url: str | None = None,
        email: str | None = None,
        normalized_email: str | None = None,
        apollo_email_status: str | None = None,
        final_status: str = FinalStatus.UNVERIFIED.value,
        source_job_id: str | None = None,
    ) -> Person:
        person = Person(
            company_id=company_id,
            apollo_person_id=apollo_person_id,
            first_name=first_name,
            last_name=last_name,
            full_name=full_name,
            job_title=job_title,
            linkedin_url=linkedin_url,
            email=email,
            normalized_email=normalized_email,
            apollo_email_status=apollo_email_status,
            final_status=final_status,
            source_job_id=source_job_id,
        )
        self._session.add(person)
        return person
