"""In-process quick pipeline for local UI (no Celery required for small CSVs)."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings, get_settings, reload_settings
from app.database.enums import JobStatus
from app.repositories.company_repository import CompanyRepository
from app.schemas.company import CompanyImportSummary
from app.schemas.job import JobCreateRequest, JobProgressResponse, JobRead
from app.services.company_service import CompanyService
from app.services.export_service import ExportService
from app.services.job_service import JobService
from app.services.job_title_profiles import resolve_job_titles
from app.utils.domain import is_name_lookup_domain
from app.utils.logging import get_logger

logger = get_logger(__name__)

# Prevent double-running the same quick-run job after refresh/resume.
_active_processors: set[str] = set()
_active_lock = threading.Lock()

_TERMINAL_JOB_STATUSES = {
    JobStatus.COMPLETED.value,
    JobStatus.FAILED.value,
    JobStatus.CANCELLED.value,
}

class QuickPipelineService:
    """Import CSV + create job, then process in the API process for small pilots."""

    def __init__(self, session: Session, *, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._companies = CompanyService(session)
        self._company_repo = CompanyRepository(session)
        self._jobs = JobService(session)

    def start_from_csv(
        self,
        content: bytes,
        *,
        filename: str,
        job_name: str = "ui-quick-run",
        job_title_profile: str | None = "custom",
        max_pages: int = 1,
        run_external_verification: bool = True,
        pipeline_mode: str = "search_only_guess",
        skip_existing_people: bool = False,
    ) -> tuple[JobRead, CompanyImportSummary, dict[str, Any]]:
        parsed = self._companies.parse_csv(content)
        if not parsed.valid_rows:
            raise ValueError(
                "No usable companies found in CSV "
                f"(invalid={len(parsed.invalid_details)}, "
                f"duplicates={parsed.duplicate_domains})"
            )

        max_allowed = self._settings.quick_run_max_companies
        if len(parsed.valid_rows) > max_allowed:
            raise ValueError(
                f"Quick UI run supports at most {max_allowed} companies "
                f"(got {len(parsed.valid_rows)}). Split your CSV or use Celery "
                "jobs for large batches."
            )

        summary = self._companies.persist_parsed(parsed)

        company_ids: list[str] = []
        seen: set[str] = set()
        for row in parsed.valid_rows:
            company = self._company_repo.get_by_domain(row.domain)
            if company is None or company.id in seen:
                continue
            seen.add(company.id)
            company_ids.append(company.id)

        if not company_ids:
            raise ValueError("Could not resolve any companies from the CSV")

        mode = (pipeline_mode or "search_only_guess").strip().lower()
        if mode == "full":
            mode = "search_and_enrich"
        if mode not in {"search_and_enrich", "search_only_guess"}:
            raise ValueError(
                f"Unknown pipeline_mode {pipeline_mode!r}. "
                "Use 'search_only_guess' or 'search_and_enrich'."
            )
        # ZeroBounce off for UI modes unless explicitly enabled later.
        verify = False

        job = self._jobs.create_job(
            JobCreateRequest(
                name=job_name,
                company_ids=company_ids,
                job_title_profile=job_title_profile,
                max_pages=max_pages,
                run_external_verification=verify,
                pipeline_mode=mode,
                skip_existing_people=bool(skip_existing_people),
            )
        )
        # Always re-read .env so TARGET_JOB_TITLES edits apply without a full
        # process restart (uvicorn --reload does not watch .env).
        self._settings = reload_settings()
        resolved_titles = resolve_job_titles(
            job_title_profile, settings=self._settings
        )
        options = {
            "job_title_profile": job_title_profile,
            # Snapshotted at start — used by the worker even if .env changes mid-run.
            "job_titles": resolved_titles,
            "max_pages": max_pages,
            "run_external_verification": verify,
            "pipeline_mode": mode,
            "skip_existing_people": bool(skip_existing_people),
            # Exact titles only — similar-title matching is much slower on Apollo.
            "include_similar_titles": False,
            "per_page": 25,
            "batch_size": self._settings.batch_size,
        }
        logger.info(
            "quick_pipeline_titles_resolved",
            extra={
                "job_id": job.id,
                "job_title_profile": job_title_profile,
                "job_titles": resolved_titles,
            },
        )
        entity = self._jobs.get_job(job.id)
        assert entity is not None
        entity.status = JobStatus.RUNNING.value
        entity.options_json = json.dumps(options)
        if entity.started_at is None:
            entity.started_at = datetime.now(timezone.utc)
        self._session.commit()
        self._session.refresh(entity)
        return JobRead.model_validate(entity), summary, options

    def prepare_continue(self, job_id: str) -> dict[str, Any]:
        """
        Prepare to continue an interrupted UI job after refresh/server restart.

        Returns whether a background worker should be started. Safe to call
        repeatedly — only one in-process worker runs per job_id.
        """
        job = self._jobs.get_job(job_id)
        if job is None:
            raise LookupError(f"Job {job_id} not found")

        if job.status in _TERMINAL_JOB_STATUSES:
            return {
                "job_id": job_id,
                "status": job.status,
                "should_start": False,
                "options": {},
                "message": f"Job already {job.status}",
            }

        try:
            options = json.loads(job.options_json or "{}")
        except json.JSONDecodeError:
            options = {}
        if not isinstance(options, dict):
            options = {}

        # Ensure ZeroBounce stays off for UI continues.
        options["run_external_verification"] = False

        if job.status != JobStatus.RUNNING.value:
            job.status = JobStatus.RUNNING.value
            if job.started_at is None:
                job.started_at = datetime.now(timezone.utc)
            job.completed_at = None
            job.options_json = json.dumps(options)
            self._session.commit()
            self._session.refresh(job)

        with _active_lock:
            already = job_id in _active_processors

        return {
            "job_id": job_id,
            "status": job.status,
            "should_start": not already,
            "options": options,
            "message": (
                "Worker already running"
                if already
                else "Continuing job from last checkpoint"
            ),
        }

    async def process_until_done(self, job_id: str, options: dict[str, Any]) -> None:
        # Fresh session for background work (request session will be closed).
        from app.database.session import get_session_factory

        with _active_lock:
            if job_id in _active_processors:
                logger.info(
                    "quick_pipeline_already_active",
                    extra={"job_id": job_id},
                )
                return
            _active_processors.add(job_id)

        session = get_session_factory()()
        try:
            jobs = JobService(session, settings=self._settings)
            logger.info("quick_pipeline_started", extra={"job_id": job_id})
            while True:
                summary = await jobs.process_next_batch(job_id, options=options)
                if summary.get("stopped"):
                    logger.info(
                        "quick_pipeline_stopped",
                        extra={"job_id": job_id, "status": summary.get("status")},
                    )
                    return
        finally:
            session.close()
            with _active_lock:
                _active_processors.discard(job_id)

    def get_view(self, job_id: str) -> dict[str, Any]:
        progress: JobProgressResponse = self._jobs.get_progress(job_id)
        results = self._jobs.get_results(job_id)
        export_meta = None
        if progress.job.status in {
            JobStatus.COMPLETED.value,
            JobStatus.FAILED.value,
        }:
            try:
                export_meta = ExportService(self._session).export_job(job_id).model_dump()
            except Exception:  # noqa: BLE001
                export_meta = None

        from sqlalchemy import select

        from app.database.models import Company
        from app.repositories.verification_repository import VerificationRepository

        verifications = VerificationRepository(self._session)
        company_ids = {
            person.company_id
            for person in [*results.accepted_people, *results.rejected_people]
        }
        companies: dict[str, Company] = {}
        if company_ids:
            companies = {
                row.id: row
                for row in self._session.scalars(
                    select(Company).where(Company.id.in_(list(company_ids)))
                ).all()
            }

        def enrich(person) -> dict[str, Any]:
            company = companies.get(person.company_id)
            latest = verifications.latest_for_person(person.id)
            payload = person.model_dump(mode="json")
            payload["company_domain"] = (
                None
                if not company or is_name_lookup_domain(company.domain)
                else company.domain
            )
            # Display/export name is Apollo Search organization.name only.
            payload["company_name"] = company.name if company else None
            payload["external_verification_status"] = latest.status if latest else None
            reason = person.rejection_reason or ""
            if reason.startswith("guess_candidates:"):
                payload["email_guess_candidates"] = reason.removeprefix(
                    "guess_candidates:"
                ).replace("|", "; ")
            else:
                payload["email_guess_candidates"] = None
            return payload

        try:
            entity = self._jobs.get_job(job_id)
            raw_options = entity.options_json if entity is not None else None
            options = json.loads(raw_options or "{}")
        except (json.JSONDecodeError, TypeError, AttributeError):
            options = {}
        job_titles = options.get("job_titles") if isinstance(options, dict) else None

        return {
            "progress": progress.model_dump(mode="json"),
            "accepted_people": [enrich(p) for p in results.accepted_people],
            "guessed_people": [enrich(p) for p in results.guessed_people],
            "rejected_people": [enrich(p) for p in results.rejected_people],
            "failed_items": [i.model_dump(mode="json") for i in results.failed_items],
            "people_found": (
                len(results.accepted_people)
                + len(results.guessed_people)
                + len(results.rejected_people)
            ),
            "export": export_meta,
            "job_titles": job_titles if isinstance(job_titles, list) else [],
        }
