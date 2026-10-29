"""Backfill company_name on accepted_contacts CSVs via Apollo Search (0 credits)."""

from __future__ import annotations

import base64
import io
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.models import Company, Person
from app.schemas.apollo import ApolloPeopleSearchRequest
from app.services.apollo import ApolloClient
from app.services.job_title_profiles import resolve_job_titles
from app.utils.domain import normalize_domain
from app.utils.email import normalize_email
from app.utils.logging import get_logger

logger = get_logger(__name__)

REQUIRED_COLUMNS = {"company_domain", "employee_name", "email"}

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


@dataclass
class CompanyNameBackfillResult:
    total_rows: int = 0
    filled: int = 0
    already_exact: int = 0
    unresolved: int = 0
    domains_searched: int = 0
    domains_total: int = 0
    apollo_calls: int = 0
    csv_bytes: bytes = b""
    notes: list[str] = field(default_factory=list)

    def to_progress(
        self,
        *,
        phase: str,
        current_domain: str | None = None,
        percent: float | None = None,
        domain_index: int | None = None,
    ) -> dict[str, Any]:
        domains_total = max(self.domains_total, 1)
        if domain_index is None:
            domain_index = self.domains_searched
        if percent is None:
            percent = round((domain_index / domains_total) * 100.0, 1)
        return {
            "phase": phase,
            "current_domain": current_domain,
            "total_rows": self.total_rows,
            "filled": self.filled,
            "already_exact": self.already_exact,
            "unresolved": self.unresolved,
            "domains_searched": self.domains_searched,
            "domain_index": domain_index,
            "domains_total": self.domains_total,
            "apollo_calls": self.apollo_calls,
            "percent": percent,
        }


class CompanyNameBackfillService:
    """
    Fill ``company_name`` from Apollo Search ``organization.name``.

    Uses People API Search only (no enrichment credits).
    """

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        apollo_client: ApolloClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._apollo = apollo_client

    async def backfill_csv(
        self,
        content: bytes,
        *,
        max_pages_per_domain: int = 5,
        per_page: int = 25,
        on_progress: ProgressCallback | None = None,
    ) -> CompanyNameBackfillResult:
        if not content or not content.strip():
            raise ValueError("Uploaded CSV is empty")

        try:
            frame = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Unable to parse CSV: {exc}") from exc

        if frame.empty:
            raise ValueError("Uploaded CSV has no rows")

        columns = {str(c).strip().lower(): c for c in frame.columns}
        missing = REQUIRED_COLUMNS - set(columns)
        if missing:
            raise ValueError(
                "CSV must include columns: "
                + ", ".join(sorted(REQUIRED_COLUMNS))
                + f" (missing: {', '.join(sorted(missing))})"
            )

        if "company_name" not in columns:
            frame["company_name"] = ""
            columns["company_name"] = "company_name"

        domain_col = columns["company_domain"]
        name_col = columns["employee_name"]
        email_col = columns["email"]
        company_name_col = columns["company_name"]
        title_col = columns.get("job_title")

        frame["_domain"] = frame[domain_col].map(self._safe_domain)
        frame["_email"] = frame[email_col].map(
            lambda v: normalize_email(str(v) or "") or ""
        )
        frame["_first"] = frame[name_col].map(self._first_name)
        if title_col:
            frame["_title"] = frame[title_col].map(
                lambda v: str(v or "").strip().lower()
            )
        else:
            frame["_title"] = ""

        result = CompanyNameBackfillResult(total_rows=len(frame))
        job_titles = resolve_job_titles("custom", settings=self._settings)

        by_domain: dict[str, list[int]] = defaultdict(list)
        for idx, domain in frame["_domain"].items():
            if domain:
                by_domain[domain].append(int(idx))

        result.domains_total = len(by_domain)
        await self._emit(
            on_progress,
            result.to_progress(phase="starting", percent=0),
        )

        apollo = self._apollo or ApolloClient(settings=self._settings)
        owns_client = self._apollo is None
        try:
            for domain, indices in by_domain.items():
                domain_index = result.domains_searched + 1
                await self._emit(
                    on_progress,
                    result.to_progress(
                        phase="searching",
                        current_domain=domain,
                        domain_index=domain_index,
                        percent=round(
                            ((domain_index - 0.5) / max(result.domains_total, 1))
                            * 100.0,
                            1,
                        ),
                    ),
                )

                emails = {
                    frame.at[i, "_email"]
                    for i in indices
                    if frame.at[i, "_email"]
                }
                id_to_org, name_title_to_org, calls = await self._search_org_names(
                    apollo,
                    domain=domain,
                    emails=emails,
                    job_titles=job_titles,
                    max_pages=max_pages_per_domain,
                    per_page=per_page,
                )
                result.apollo_calls += calls
                result.domains_searched += 1

                email_to_apollo = self._email_to_apollo_id(domain, emails)

                for i in indices:
                    email = frame.at[i, "_email"]
                    apollo_id = email_to_apollo.get(email)
                    org = None
                    if apollo_id:
                        org = id_to_org.get(apollo_id)
                    if not org:
                        key = (frame.at[i, "_first"], frame.at[i, "_title"])
                        org = name_title_to_org.get(key)
                    if not org and frame.at[i, "_first"]:
                        matches = [
                            name
                            for (first, _title), name in name_title_to_org.items()
                            if first == frame.at[i, "_first"] and name
                        ]
                        uniq = {m for m in matches if m}
                        if len(uniq) == 1:
                            org = next(iter(uniq))

                    current = str(frame.at[i, company_name_col] or "").strip()
                    if org:
                        if current.lower() == org.lower():
                            result.already_exact += 1
                        else:
                            frame.at[i, company_name_col] = org
                            result.filled += 1
                    else:
                        result.unresolved += 1

                await self._emit(
                    on_progress,
                    result.to_progress(
                        phase="domain_done",
                        current_domain=domain,
                    ),
                )
        finally:
            if owns_client:
                await apollo.aclose()

        out = frame.drop(columns=[c for c in frame.columns if str(c).startswith("_")])
        buffer = io.BytesIO()
        out.to_csv(buffer, index=False)
        result.csv_bytes = buffer.getvalue()
        result.notes.append(
            "Filled from Apollo People Search organization.name (0 enrichment credits)."
        )
        await self._emit(
            on_progress,
            {
                **result.to_progress(phase="completed", percent=100),
                "filename": "accepted_contacts_apollo_company_names.csv",
                "csv_base64": base64.b64encode(result.csv_bytes).decode("ascii"),
                "notes": result.notes,
            },
        )
        return result

    async def _emit(
        self,
        on_progress: ProgressCallback | None,
        payload: dict[str, Any],
    ) -> None:
        if on_progress is None:
            return
        maybe = on_progress(payload)
        if maybe is not None and hasattr(maybe, "__await__"):
            await maybe  # type: ignore[misc]

    def _email_to_apollo_id(self, domain: str, emails: set[str]) -> dict[str, str]:
        if not emails:
            return {}
        company = self._session.scalar(select(Company).where(Company.domain == domain))
        if company is None:
            return {}
        rows = self._session.scalars(
            select(Person).where(
                Person.company_id == company.id,
                Person.email.in_(list(emails)),
            )
        ).all()
        mapping: dict[str, str] = {}
        for person in rows:
            email = normalize_email(person.email or "") or ""
            if email and person.apollo_person_id:
                mapping[email] = person.apollo_person_id
        return mapping

    async def _search_org_names(
        self,
        apollo: ApolloClient,
        *,
        domain: str,
        emails: set[str],
        job_titles: list[str],
        max_pages: int,
        per_page: int,
    ) -> tuple[dict[str, str], dict[tuple[str, str], str], int]:
        id_to_org: dict[str, str] = {}
        name_title_to_org: dict[tuple[str, str], str] = {}
        calls = 0
        needed_ids = set(self._email_to_apollo_id(domain, emails).values())

        for page in range(1, max(1, max_pages) + 1):
            request = ApolloPeopleSearchRequest(
                q_organization_domains_list=[domain],
                person_titles=job_titles,
                include_similar_titles=False,
                page=page,
                per_page=min(per_page, 100),
            )
            response = await apollo.search_people(request)
            calls += 1
            if not response.people:
                break
            for person in response.people:
                org_name = None
                if person.organization and person.organization.name:
                    org_name = person.organization.name.strip()
                if not org_name:
                    continue
                if person.id:
                    id_to_org[person.id] = org_name
                first = (person.first_name or "").strip().lower()
                title = (person.title or "").strip().lower()
                if first:
                    name_title_to_org[(first, title)] = org_name
            if needed_ids and needed_ids.issubset(id_to_org):
                break

        logger.info(
            "company_name_backfill_domain",
            extra={
                "domain": domain,
                "apollo_calls": calls,
                "orgs_by_id": len(id_to_org),
                "needed_ids": len(needed_ids),
                "resolved_needed": len(needed_ids & set(id_to_org)),
            },
        )
        return id_to_org, name_title_to_org, calls

    @staticmethod
    def _safe_domain(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            return normalize_domain(raw)
        except Exception:  # noqa: BLE001
            return raw.lower().removeprefix("www.")

    @staticmethod
    def _first_name(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text.split()[0].lower()
