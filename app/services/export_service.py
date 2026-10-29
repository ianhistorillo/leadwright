"""CSV export service for accepted/rejected contacts and processing errors."""

from __future__ import annotations

from pathlib import Path

import json

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.enums import FinalStatus
from app.database.models import Company, Person, ProcessingError
from app.repositories.job_repository import JobRepository
from app.repositories.verification_repository import VerificationRepository
from app.schemas.export import JobExportResponse
from app.utils.domain import is_name_lookup_domain
from app.utils.logging import get_logger

logger = get_logger(__name__)

ACCEPTED_COLUMNS = [
    "company_name",
    "company_domain",
    "employee_name",
    "job_title",
    "email",
    "email_guess_candidates",
    "apollo_email_status",
    "external_verification_status",
    "linkedin_url",
    "final_status",
]

REJECTED_COLUMNS = [
    *ACCEPTED_COLUMNS,
    "rejection_reason",
]

ERROR_COLUMNS = [
    "job_id",
    "job_item_id",
    "company_id",
    "provider",
    "operation",
    "error_type",
    "message",
    "http_status",
    "retryable",
    "attempt",
    "created_at",
]


class ExportService:
    """Build clean CSV exports for a completed (or in-progress) job."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        output_root: Path | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._jobs = JobRepository(session)
        self._verifications = VerificationRepository(session)
        root = output_root or Path("data") / "output"
        self._output_root = root.resolve()

    def export_job(self, job_id: str) -> JobExportResponse:
        job = self._jobs.get_job(job_id)
        if job is None:
            raise LookupError(f"Job not found: {job_id}")

        company_ids = [
            item.company_id
            for item in self._jobs.list_items(job_id)
        ]
        companies = self._load_companies(company_ids)
        people = self._load_people(company_ids)
        # Always export people stamped by this job (same scope as UI results).
        try:
            options = json.loads(job.options_json or "{}")
        except Exception:  # noqa: BLE001
            options = {}
        people = [p for p in people if p.source_job_id == job_id]
        title_filters = options.get("job_titles") if isinstance(options, dict) else None
        if isinstance(title_filters, list) and title_filters:
            from app.services.people_service import PeopleService

            people = [
                p
                for p in people
                if PeopleService._title_matches_filters(
                    p.job_title, title_filters, include_similar=False
                )
            ]
        errors = self._load_errors(job_id)

        accepted_rows: list[dict[str, object]] = []
        rejected_rows: list[dict[str, object]] = []

        for person in people:
            company = companies.get(person.company_id)
            external_status = self._latest_external_status(person.id)
            guess_candidates = ""
            if (person.apollo_email_status or "").lower() == "guessed":
                reason = person.rejection_reason or ""
                if reason.startswith("guess_candidates:"):
                    guess_candidates = reason.removeprefix("guess_candidates:").replace(
                        "|", "; "
                    )
            row = {
                "company_name": ((company.name if company else None) or ""),
                "company_domain": (
                    ""
                    if not company
                    or is_name_lookup_domain(company.domain)
                    else (company.domain or "")
                ),
                "employee_name": person.full_name
                or " ".join(
                    part for part in [person.first_name, person.last_name] if part
                )
                or "",
                "job_title": person.job_title or "",
                "email": person.email or "",
                "email_guess_candidates": guess_candidates,
                "apollo_email_status": person.apollo_email_status or "",
                "external_verification_status": external_status or "",
                "linkedin_url": person.linkedin_url or "",
                "final_status": person.final_status,
            }
            is_guessed = (person.apollo_email_status or "").lower() == "guessed"
            if person.final_status == FinalStatus.ACCEPTED.value or is_guessed:
                accepted_rows.append(row)
            else:
                rejected_rows.append(
                    {
                        **row,
                        "rejection_reason": person.rejection_reason or "",
                    }
                )

        error_rows = [
            {
                "job_id": err.job_id or "",
                "job_item_id": err.job_item_id or "",
                "company_id": err.company_id or "",
                "provider": err.provider or "",
                "operation": err.operation,
                "error_type": err.error_type,
                "message": err.message,
                "http_status": err.http_status if err.http_status is not None else "",
                "retryable": err.retryable,
                "attempt": err.attempt,
                "created_at": err.created_at.isoformat() if err.created_at else "",
            }
            for err in errors
        ]

        out_dir = self._output_root / job_id
        out_dir.mkdir(parents=True, exist_ok=True)

        accepted_path = out_dir / "accepted_contacts.csv"
        rejected_path = out_dir / "rejected_contacts.csv"
        errors_path = out_dir / "processing_errors.csv"

        self._write_csv(accepted_path, accepted_rows, ACCEPTED_COLUMNS)
        self._write_csv(rejected_path, rejected_rows, REJECTED_COLUMNS)
        self._write_csv(errors_path, error_rows, ERROR_COLUMNS)

        logger.info(
            "job_export_written",
            extra={
                "job_id": job_id,
                "accepted_count": len(accepted_rows),
                "rejected_count": len(rejected_rows),
                "processing_errors_count": len(error_rows),
                "output_dir": str(out_dir),
            },
        )

        files = [
            accepted_path.name,
            rejected_path.name,
            errors_path.name,
        ]
        return JobExportResponse(
            job_id=job_id,
            output_dir=str(out_dir),
            accepted_contacts_path=str(accepted_path),
            rejected_contacts_path=str(rejected_path),
            processing_errors_path=str(errors_path),
            accepted_count=len(accepted_rows),
            rejected_count=len(rejected_rows),
            processing_errors_count=len(error_rows),
            files=files,
        )

    def resolve_export_file(self, job_id: str, filename: str) -> Path:
        """Resolve a safe path under the job export directory."""
        allowed = {
            "accepted_contacts.csv",
            "rejected_contacts.csv",
            "processing_errors.csv",
        }
        if filename not in allowed:
            raise ValueError(f"Unsupported export file: {filename}")
        path = (self._output_root / job_id / filename).resolve()
        if not str(path).startswith(str(self._output_root)):
            raise ValueError("Invalid export path")
        if not path.is_file():
            raise FileNotFoundError(
                f"Export file not found for job {job_id}: {filename}. "
                "Call GET /jobs/{id}/export first."
            )
        return path

    def _load_companies(self, company_ids: list[str]) -> dict[str, Company]:
        if not company_ids:
            return {}
        rows = self._session.scalars(
            select(Company).where(Company.id.in_(company_ids))
        ).all()
        return {row.id: row for row in rows}

    def _load_people(self, company_ids: list[str]) -> list[Person]:
        if not company_ids:
            return []
        stmt = (
            select(Person)
            .where(Person.company_id.in_(company_ids))
            .order_by(Person.created_at.asc())
        )
        return list(self._session.scalars(stmt).all())

    def _load_errors(self, job_id: str) -> list[ProcessingError]:
        stmt = (
            select(ProcessingError)
            .where(ProcessingError.job_id == job_id)
            .order_by(ProcessingError.created_at.asc())
        )
        return list(self._session.scalars(stmt).all())

    def _latest_external_status(self, person_id: str) -> str | None:
        latest = self._verifications.latest_for_person(person_id)
        return latest.status if latest else None

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, object]], columns: list[str]) -> None:
        frame = pd.DataFrame(rows, columns=columns)
        if frame.empty:
            frame = pd.DataFrame(columns=columns)
        frame.to_csv(path, index=False)
