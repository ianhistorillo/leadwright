"""Company import and query services."""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy.orm import Session

from app.database.enums import CompanyStatus
from app.database.models import Company
from app.repositories.company_repository import CompanyRepository
from app.schemas.company import (
    CompanyImportSummary,
    CompanyListResponse,
    CompanyRead,
    InvalidImportRow,
)
from app.utils.domain import (
    DomainNormalizationError,
    is_name_lookup_domain,
    lookup_domain_for_name,
    normalize_domain,
    website_from_domain,
)

ALLOWED_COLUMNS = {"company_website", "company_domain", "company_name"}


@dataclass
class ParsedCompanyRow:
    """A successfully normalized company row from CSV."""

    row_number: int
    domain: str
    website: str | None
    # CSV company_name — search hint only; never treated as Apollo display name.
    lookup_name: str | None = None
    source_value: str | None = None


@dataclass
class CsvParseResult:
    """Intermediate CSV validation result before persistence."""

    total_rows: int = 0
    valid_rows: list[ParsedCompanyRow] = field(default_factory=list)
    invalid_details: list[InvalidImportRow] = field(default_factory=list)
    duplicate_domains: int = 0


class CompanyService:
    """Business logic for company CSV import and retrieval."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = CompanyRepository(session)

    def get_company(self, company_id: str) -> Company | None:
        return self._repo.get_by_id(company_id)

    def list_companies(self, *, offset: int = 0, limit: int = 100) -> CompanyListResponse:
        items = self._repo.list_companies(offset=offset, limit=limit)
        return CompanyListResponse(
            total=self._repo.count(),
            items=[CompanyRead.model_validate(item) for item in items],
        )

    def import_csv_bytes(self, content: bytes, *, filename: str | None = None) -> CompanyImportSummary:
        """Validate, normalize, dedupe, and persist companies from a CSV upload."""
        if filename and not filename.lower().endswith(".csv"):
            raise ValueError("Only .csv files are allowed")

        parse_result = self.parse_csv(content)
        return self.persist_parsed(parse_result)

    def parse_csv(self, content: bytes) -> CsvParseResult:
        """Parse and validate CSV content without writing to the database."""
        if not content or not content.strip():
            raise ValueError("Uploaded CSV is empty")

        try:
            frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001 — surface parse errors to API
            raise ValueError(f"Unable to parse CSV: {exc}") from exc

        if frame.empty and list(frame.columns) == []:
            raise ValueError("Uploaded CSV is empty")

        columns = {str(col).strip().lower(): col for col in frame.columns}
        has_website = "company_website" in columns
        has_domain = "company_domain" in columns
        has_name = "company_name" in columns
        if not has_website and not has_domain and not has_name:
            raise ValueError(
                "CSV must include 'company_name' and/or 'company_website' "
                "(optional: 'company_domain')"
            )

        # Allow unknown columns but ignore them; do not fail the import.

        result = CsvParseResult(total_rows=len(frame))
        seen_domains: dict[str, int] = {}

        for index, row in frame.iterrows():
            row_number = int(index) + 1
            website_raw = (
                str(row[columns["company_website"]]).strip() if has_website else ""
            )
            domain_raw = (
                str(row[columns["company_domain"]]).strip() if has_domain else ""
            )
            name_raw = (
                str(row[columns["company_name"]]).strip() if has_name else ""
            )
            lookup_name = name_raw or None

            source_value = website_raw or domain_raw or name_raw
            if not website_raw and not domain_raw and not name_raw:
                result.invalid_details.append(
                    InvalidImportRow(
                        row_number=row_number,
                        value=None,
                        reason="Missing company_name and company_website/company_domain",
                    )
                )
                continue

            website: str | None = None
            try:
                if domain_raw or website_raw:
                    # Prefer explicit domain column when present and non-empty;
                    # otherwise normalize the website.
                    if domain_raw:
                        domain = normalize_domain(domain_raw)
                    else:
                        domain = normalize_domain(website_raw)
                    # Cross-check: if both provided, website must resolve to same domain.
                    if domain_raw and website_raw:
                        website_domain = normalize_domain(website_raw)
                        if website_domain != domain:
                            raise DomainNormalizationError(
                                "company_website and company_domain do not match"
                            )
                    website = website_from_domain(domain)
                else:
                    # Name-only row — synthetic domain key; Apollo Search uses lookup_name.
                    domain = lookup_domain_for_name(name_raw)
                    website = None
            except DomainNormalizationError as exc:
                result.invalid_details.append(
                    InvalidImportRow(
                        row_number=row_number,
                        value=source_value,
                        reason=str(exc),
                    )
                )
                continue

            if domain in seen_domains:
                result.duplicate_domains += 1
                continue

            seen_domains[domain] = row_number
            result.valid_rows.append(
                ParsedCompanyRow(
                    row_number=row_number,
                    domain=domain,
                    website=website,
                    lookup_name=lookup_name,
                    source_value=source_value,
                )
            )

        return result

    def persist_parsed(self, parse_result: CsvParseResult) -> CompanyImportSummary:
        """Persist newly seen domains; skip domains already in the database."""
        domains = [row.domain for row in parse_result.valid_rows]
        existing = self._repo.list_domains(domains)

        created: list[Company] = []
        skipped_existing = 0
        for row in parse_result.valid_rows:
            if row.domain in existing:
                skipped_existing += 1
                company = self._repo.get_by_domain(row.domain)
                if company is not None:
                    # Keep CSV name as lookup hint only — never as display name.
                    if row.lookup_name and not (company.lookup_name or "").strip():
                        company.lookup_name = row.lookup_name
                    if row.website and not (company.website or "").strip():
                        if not is_name_lookup_domain(company.domain):
                            company.website = row.website
                continue
            company = Company(
                domain=row.domain,
                website=row.website,
                # Display name stays empty until Apollo Search fills organization.name.
                name=None,
                lookup_name=row.lookup_name,
                status=CompanyStatus.PENDING.value,
            )
            created.append(company)
            existing.add(row.domain)

        if created:
            self._repo.bulk_create(created)
        self._session.commit()
        for company in created:
            self._session.refresh(company)

        return CompanyImportSummary(
            total_rows=parse_result.total_rows,
            valid_companies=len(created),
            invalid_rows=len(parse_result.invalid_details),
            duplicate_domains=parse_result.duplicate_domains,
            existing_domains_skipped=skipped_existing,
            companies=[CompanyRead.model_validate(c) for c in created],
            invalid_details=parse_result.invalid_details,
        )
