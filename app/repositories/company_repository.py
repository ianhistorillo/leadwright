"""Company persistence helpers."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database.enums import CompanyStatus
from app.database.models import Company


class CompanyRepository:
    """Data-access layer for Company records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, company_id: str) -> Company | None:
        return self._session.get(Company, company_id)

    def get_by_domain(self, domain: str) -> Company | None:
        stmt = select(Company).where(Company.domain == domain)
        return self._session.scalar(stmt)

    def list_domains(self, domains: list[str]) -> set[str]:
        if not domains:
            return set()
        stmt = select(Company.domain).where(Company.domain.in_(domains))
        return set(self._session.scalars(stmt).all())

    def list_companies(self, *, offset: int = 0, limit: int = 100) -> list[Company]:
        stmt = (
            select(Company)
            .order_by(Company.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def count(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(Company)) or 0)

    def create(
        self,
        *,
        domain: str,
        website: str | None = None,
        name: str | None = None,
        status: str = CompanyStatus.PENDING.value,
    ) -> Company:
        company = Company(
            domain=domain,
            website=website,
            name=name,
            status=status,
        )
        self._session.add(company)
        return company

    def bulk_create(self, companies: list[Company]) -> list[Company]:
        self._session.add_all(companies)
        return companies
