"""Job lifecycle, checkpoint resume, and retry orchestration."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database.enums import FinalStatus, JobItemStatus, JobStatus
from app.database.models import Job, JobItem, Person, ProcessingError
from app.repositories.company_repository import CompanyRepository
from app.repositories.job_repository import JobRepository
from app.repositories.person_repository import PersonRepository
from app.schemas.job import (
    JobCreateRequest,
    JobItemRead,
    JobListResponse,
    JobProgressResponse,
    JobRead,
    JobResultsResponse,
    JobRunOptions,
)
from app.schemas.person import PersonEnrichRequest, PersonRead, PersonSearchRequest
from app.schemas.verification import PersonVerifyRequest
from app.services.people_service import PeopleService
from app.services.verification_service import VerificationService
from app.utils.errors import (
    get_retry_after_seconds,
    http_status_for_error,
    is_permanent_error,
    is_transient_error,
    provider_for_error,
)
from app.utils.logging import get_logger
from app.utils.retry import compute_backoff_seconds

logger = get_logger(__name__)

_ACTIVE_ITEM_STATUSES = {
    JobItemStatus.PENDING.value,
    JobItemStatus.SEARCHING.value,
    JobItemStatus.ENRICHING.value,
    JobItemStatus.VERIFYING.value,
}

_RESUME_ITEM_STATUSES = [
    JobItemStatus.PENDING.value,
    JobItemStatus.SEARCHING.value,
    JobItemStatus.ENRICHING.value,
    JobItemStatus.VERIFYING.value,
]

# Pipeline stage order for checkpoint resume.
_STAGE_ORDER = [
    JobItemStatus.PENDING.value,
    JobItemStatus.SEARCHING.value,
    JobItemStatus.ENRICHING.value,
    JobItemStatus.VERIFYING.value,
    JobItemStatus.COMPLETED.value,
]


class JobStateError(ValueError):
    """Invalid job state transition."""


class JobService:
    """Create and control discovery jobs; execute per-company pipeline steps."""

    def __init__(
        self,
        session: Session,
        *,
        settings: Settings | None = None,
        people_service: PeopleService | None = None,
        verification_service: VerificationService | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._jobs = JobRepository(session)
        self._companies = CompanyRepository(session)
        self._people = PersonRepository(session)
        self._people_service = people_service
        self._verification_service = verification_service

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get_job(job_id)

    def list_jobs(self, *, offset: int = 0, limit: int = 100) -> JobListResponse:
        items = self._jobs.list_jobs(offset=offset, limit=limit)
        return JobListResponse(
            total=self._jobs.count_jobs(),
            items=[JobRead.model_validate(item) for item in items],
        )

    def create_job(self, request: JobCreateRequest) -> JobRead:
        if request.company_ids:
            companies = []
            for company_id in request.company_ids:
                company = self._companies.get_by_id(company_id)
                if company is None:
                    raise LookupError(f"Company not found: {company_id}")
                companies.append(company)
        else:
            companies = self._companies.list_companies(offset=0, limit=100_000)
            if not companies:
                raise ValueError("No companies available to process")

        job = self._jobs.create_job(name=request.name, total_companies=len(companies))
        job.options_json = json.dumps(self.options_from_create(request))
        self._session.flush()
        for company in companies:
            self._jobs.create_job_item(job_id=job.id, company_id=company.id)

        self._session.commit()
        self._session.refresh(job)
        return JobRead.model_validate(job)

    def start_job(self, job_id: str, *, options: dict[str, Any] | None = None) -> JobRead:
        job = self._require_job(job_id)
        if job.status not in {JobStatus.PENDING.value, JobStatus.PAUSED.value}:
            raise JobStateError(
                f"Cannot start job in status={job.status}; expected pending or paused"
            )

        merged = self._merge_options(job, options)
        job.options_json = json.dumps(merged)
        job.status = JobStatus.RUNNING.value
        if job.started_at is None:
            job.started_at = datetime.now(timezone.utc)
        job.completed_at = None
        self._session.commit()
        self._session.refresh(job)

        from app.workers.tasks import run_job

        run_job.delay(job_id, merged)
        return JobRead.model_validate(job)

    def pause_job(self, job_id: str) -> JobRead:
        job = self._require_job(job_id)
        if job.status != JobStatus.RUNNING.value:
            raise JobStateError(f"Cannot pause job in status={job.status}")
        job.status = JobStatus.PAUSED.value
        self._session.commit()
        self._session.refresh(job)
        return JobRead.model_validate(job)

    def resume_job(self, job_id: str, *, options: dict[str, Any] | None = None) -> JobRead:
        """
        Resume a paused job from the last item checkpoint.

        In-progress item statuses (searching/enriching/verifying) are kept and
        continued; they are not reset to pending.
        """
        job = self._require_job(job_id)
        if job.status != JobStatus.PAUSED.value:
            raise JobStateError(f"Cannot resume job in status={job.status}")
        return self.start_job(job_id, options=options)

    def cancel_job(self, job_id: str) -> JobRead:
        job = self._require_job(job_id)
        if job.status in {
            JobStatus.COMPLETED.value,
            JobStatus.CANCELLED.value,
        }:
            raise JobStateError(f"Cannot cancel job in status={job.status}")
        job.status = JobStatus.CANCELLED.value
        job.completed_at = datetime.now(timezone.utc)
        self._session.commit()
        self._session.refresh(job)
        return JobRead.model_validate(job)

    def retry_failed(self, job_id: str, *, options: dict[str, Any] | None = None) -> JobRead:
        job = self._require_job(job_id)
        failed_items = self._jobs.list_items(
            job_id, statuses=[JobItemStatus.FAILED.value]
        )
        if not failed_items:
            raise ValueError("No failed job items to retry")

        for item in failed_items:
            # Restart failed items from the beginning of the pipeline.
            item.status = JobItemStatus.PENDING.value
            item.error_message = None
            item.processed_at = None
            item.retry_count += 1

        job.status = JobStatus.PENDING.value
        job.completed_at = None
        self._jobs.refresh_job_counters(job)
        self._session.commit()
        return self.start_job(job_id, options=options)

    def get_progress(self, job_id: str) -> JobProgressResponse:
        job = self._require_job(job_id)
        counts = self._jobs.count_items_by_status(job_id)
        total = max(job.total_companies, 1)
        completed = counts.get(JobItemStatus.COMPLETED.value, 0)
        failed = counts.get(JobItemStatus.FAILED.value, 0)
        searching = counts.get(JobItemStatus.SEARCHING.value, 0)
        enriching = counts.get(JobItemStatus.ENRICHING.value, 0)
        verifying = counts.get(JobItemStatus.VERIFYING.value, 0)
        # Count in-flight companies as partial progress so the UI moves during long Apollo calls.
        partial = 0.35 * searching + 0.7 * enriching + 0.85 * verifying
        processed = completed + failed
        percent = min(100.0, round(((processed + partial) / total) * 100.0, 2))
        # Keep stored counters fresh for API consumers.
        job.successful_companies = completed
        job.failed_companies = failed
        job.processed_companies = processed
        return JobProgressResponse(
            job=JobRead.model_validate(job),
            pending=counts.get(JobItemStatus.PENDING.value, 0),
            searching=searching,
            enriching=enriching,
            verifying=verifying,
            completed=completed,
            failed=failed,
            percent_complete=percent,
        )

    def get_results(self, job_id: str) -> JobResultsResponse:
        job = self._require_job(job_id)
        items = self._jobs.list_items(job_id)
        company_ids = [item.company_id for item in items]
        people = (
            self._session.scalars(
                select(Person).where(Person.company_id.in_(company_ids))
            ).all()
            if company_ids
            else []
        )
        options = self._merge_options(job, None)
        # Always scope results to people stamped by this job. "skip_existing_people"
        # only controls search upserts — when false, the old behavior mixed in every
        # historical person on those companies (HR/Engineers from prior title lists).
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

        accepted = [
            PersonRead.model_validate(p)
            for p in people
            if p.final_status == FinalStatus.ACCEPTED.value
        ]
        guessed = [
            PersonRead.model_validate(p)
            for p in people
            if (p.apollo_email_status or "").lower() == "guessed"
            and p.final_status != FinalStatus.ACCEPTED.value
        ]
        guessed_ids = {p.id for p in guessed}
        rejected = [
            PersonRead.model_validate(p)
            for p in people
            if p.final_status != FinalStatus.ACCEPTED.value
            and p.id not in guessed_ids
        ]
        failed_items = [
            JobItemRead.model_validate(item)
            for item in items
            if item.status == JobItemStatus.FAILED.value
        ]
        return JobResultsResponse(
            job_id=job.id,
            accepted_people=accepted,
            guessed_people=guessed,
            rejected_people=rejected,
            failed_items=failed_items,
        )

    def list_processing_errors(self, job_id: str, *, limit: int = 100) -> list[ProcessingError]:
        self._require_job(job_id)
        stmt = (
            select(ProcessingError)
            .where(ProcessingError.job_id == job_id)
            .order_by(ProcessingError.created_at.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    async def process_next_batch(
        self,
        job_id: str,
        *,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process up to BATCH_SIZE resumable items for a running job.

        Includes PENDING and in-progress checkpoint statuses so interrupted
        workers can continue without redoing completed stages.
        """
        job = self._require_job(job_id)
        options = self._merge_options(job, options)

        if job.status == JobStatus.PAUSED.value:
            return {"status": job.status, "processed": 0, "stopped": True}
        if job.status == JobStatus.CANCELLED.value:
            return {"status": job.status, "processed": 0, "stopped": True}
        if job.status != JobStatus.RUNNING.value:
            return {"status": job.status, "processed": 0, "stopped": True}

        batch_size = int(options.get("batch_size") or self._settings.batch_size)
        items = self._jobs.list_items(
            job_id,
            statuses=_RESUME_ITEM_STATUSES,
            limit=batch_size,
        )
        if not items:
            self._finalize_if_done(job)
            self._session.commit()
            return {
                "status": job.status,
                "processed": 0,
                "stopped": True,
                "done": True,
            }

        processed = 0
        for item in items:
            self._session.refresh(job)
            if job.status in {JobStatus.PAUSED.value, JobStatus.CANCELLED.value}:
                break
            await self._process_job_item(job, item, options=options)
            processed += 1

        self._jobs.refresh_job_counters(job)
        self._finalize_if_done(job)
        self._session.commit()
        return {
            "status": job.status,
            "processed": processed,
            "stopped": job.status
            in {
                JobStatus.PAUSED.value,
                JobStatus.CANCELLED.value,
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
            },
            "done": job.status
            in {
                JobStatus.COMPLETED.value,
                JobStatus.FAILED.value,
                JobStatus.CANCELLED.value,
            },
        }

    async def _process_job_item(
        self,
        job: Job,
        item: JobItem,
        *,
        options: dict[str, Any],
    ) -> None:
        people = self._people_service or PeopleService(
            self._session, settings=self._settings
        )
        verification = self._verification_service or VerificationService(
            self._session, settings=self._settings
        )

        search_request = PersonSearchRequest(
            job_title_profile=options.get("job_title_profile"),
            job_titles=options.get("job_titles"),
            contact_email_status=options.get("contact_email_status"),
            include_similar_titles=bool(
                options.get("include_similar_titles", False)
            ),
            max_pages=int(options.get("max_pages") or 2),
            per_page=int(options.get("per_page") or 25),
            skip_existing_people=bool(options.get("skip_existing_people", False)),
            source_job_id=job.id,
        )
        run_verify = bool(options.get("run_external_verification", False))
        pipeline_mode = str(
            options.get("pipeline_mode") or "search_and_enrich"
        ).strip().lower()
        max_attempts = max(1, int(self._settings.max_retries) + 1)

        # Capture before any DB work — after a failed flush these attrs expire
        # and reading them raises PendingRollbackError (kills the whole job).
        job_id = job.id
        item_id = item.id
        company_id = item.company_id

        attempt = 0
        while attempt < max_attempts:
            try:
                await self._run_pipeline_from_checkpoint(
                    job,
                    item,
                    people=people,
                    verification=verification,
                    search_request=search_request,
                    run_verify=run_verify,
                    pipeline_mode=pipeline_mode,
                    skip_existing_people=bool(
                        options.get("skip_existing_people", False)
                    ),
                )
                self._jobs.mark_item(
                    item,
                    status=JobItemStatus.COMPLETED.value,
                    error_message=None,
                )
                self._checkpoint()
                return
            except Exception as exc:  # noqa: BLE001
                checkpoint = None
                try:
                    checkpoint = item.status
                except Exception:  # noqa: BLE001
                    checkpoint = None

                try:
                    self._session.rollback()
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "job_item_rollback_failed",
                        extra={"job_id": job_id, "job_item_id": item_id},
                    )

                attempt += 1
                job = self._require_job(job_id)
                item = self._session.get(JobItem, item_id)
                if item is None:
                    logger.error(
                        "job_item_missing_after_rollback",
                        extra={"job_id": job_id, "job_item_id": item_id},
                    )
                    return

                retryable = is_transient_error(exc) and not is_permanent_error(exc)
                operation = item.status or checkpoint or "pipeline"
                self._record_processing_error(
                    job=job,
                    item=item,
                    operation=operation,
                    exc=exc,
                    attempt=attempt,
                    retryable=retryable,
                )
                logger.exception(
                    "job_item_attempt_failed",
                    extra={
                        "job_id": job_id,
                        "job_item_id": item_id,
                        "company_id": company_id,
                        "attempt": attempt,
                        "retryable": retryable,
                        "checkpoint": item.status,
                    },
                )

                if retryable and attempt < max_attempts:
                    item.retry_count += 1
                    delay = compute_backoff_seconds(
                        attempt - 1,
                        retry_after=get_retry_after_seconds(exc),
                    )
                    self._checkpoint()
                    await asyncio.sleep(delay)
                    continue

                self._jobs.mark_item(
                    item,
                    status=JobItemStatus.FAILED.value,
                    error_message=str(exc)[:2000],
                    increment_retry=not retryable,
                )
                self._checkpoint()
                return

    async def _run_pipeline_from_checkpoint(
        self,
        job: Job,
        item: JobItem,
        *,
        people: PeopleService,
        verification: VerificationService,
        search_request: PersonSearchRequest,
        run_verify: bool,
        pipeline_mode: str = "search_and_enrich",
        skip_existing_people: bool = False,
    ) -> None:
        """Continue a company pipeline from the item's current checkpoint status."""
        stage = item.status or JobItemStatus.PENDING.value
        mode = (pipeline_mode or "search_and_enrich").strip().lower()
        if mode == "full":
            mode = "search_and_enrich"
        search_only_guess = mode == "search_only_guess"
        # Always stamp new people with this job id so "new only" export can filter.
        # When skip_existing is off, export still includes all company people.
        new_only_scope = job.id if skip_existing_people else None

        if self._should_run_stage(stage, JobItemStatus.SEARCHING.value):
            logger.info(
                "job_item_search",
                extra={
                    "job_id": job.id,
                    "job_item_id": item.id,
                    "company_id": item.company_id,
                    "resume_from": stage,
                    "pipeline_mode": mode,
                    "skip_existing_people": skip_existing_people,
                },
            )
            self._jobs.mark_item(
                item,
                status=JobItemStatus.SEARCHING.value,
                error_message=None,
            )
            self._checkpoint()
            await people.search_company_people(item.company_id, search_request)
            stage = JobItemStatus.SEARCHING.value

        if search_only_guess:
            # Skip Apollo enrichment + external verification (0 enrich credits).
            people.apply_email_guesses_for_company(
                item.company_id,
                source_job_id=new_only_scope,
            )
            return

        if self._should_run_stage(stage, JobItemStatus.ENRICHING.value):
            logger.info(
                "job_item_enrich",
                extra={
                    "job_id": job.id,
                    "job_item_id": item.id,
                    "company_id": item.company_id,
                },
            )
            self._jobs.mark_item(item, status=JobItemStatus.ENRICHING.value)
            self._checkpoint()
            await people.enrich_people(
                company_id=item.company_id,
                request=PersonEnrichRequest(
                    only_missing_email=True,
                    limit=500,
                    source_job_id=new_only_scope,
                ),
            )
            stage = JobItemStatus.ENRICHING.value

        # ZeroBounce disabled by default. Only run when explicitly requested AND enabled.
        if run_verify and self._settings.email_verifier_enabled:
            if self._should_run_stage(stage, JobItemStatus.VERIFYING.value):
                logger.info(
                    "job_item_verify",
                    extra={
                        "job_id": job.id,
                        "job_item_id": item.id,
                        "company_id": item.company_id,
                    },
                )
                self._jobs.mark_item(item, status=JobItemStatus.VERIFYING.value)
                self._checkpoint()
                await verification.verify_people(
                    company_id=item.company_id,
                    request=PersonVerifyRequest(only_unverified=True, limit=500),
                )
        else:
            # Search + Apollo emails (credits): accept Apollo-verified contacts as-is.
            people.accept_apollo_passed_without_external(
                item.company_id,
                source_job_id=new_only_scope,
            )

    @staticmethod
    def _should_run_stage(current_status: str, target_stage: str) -> bool:
        """
        Decide whether `target_stage` still needs to run.

        PENDING/SEARCHING => run search, enrich, verify
        ENRICHING => skip search; run enrich + verify
        VERIFYING => skip search/enrich; run verify
        """
        order = {
            JobItemStatus.PENDING.value: 0,
            JobItemStatus.SEARCHING.value: 1,
            JobItemStatus.ENRICHING.value: 2,
            JobItemStatus.VERIFYING.value: 3,
            JobItemStatus.COMPLETED.value: 4,
            JobItemStatus.FAILED.value: 0,
        }
        target_order = {
            JobItemStatus.SEARCHING.value: 1,
            JobItemStatus.ENRICHING.value: 2,
            JobItemStatus.VERIFYING.value: 3,
        }
        current = order.get(current_status, 0)
        needed = target_order[target_stage]
        # If we are currently ON the target stage (interrupted mid-stage), re-run it.
        if current_status == target_stage:
            return True
        # If we haven't reached this stage yet, run it.
        return current < needed

    def _checkpoint(self) -> None:
        """Persist progress so interrupted workers can resume."""
        self._session.commit()

    def _record_processing_error(
        self,
        *,
        job: Job,
        item: JobItem,
        operation: str,
        exc: BaseException,
        attempt: int,
        retryable: bool,
    ) -> None:
        row = ProcessingError(
            job_id=job.id,
            job_item_id=item.id,
            company_id=item.company_id,
            provider=provider_for_error(exc),
            operation=operation,
            error_type=type(exc).__name__,
            message=str(exc)[:2000],
            http_status=http_status_for_error(exc),
            retryable=retryable,
            attempt=attempt,
        )
        self._session.add(row)

    def _merge_options(
        self,
        job: Job,
        options: dict[str, Any] | None,
    ) -> dict[str, Any]:
        stored: dict[str, Any] = {}
        if job.options_json:
            try:
                loaded = json.loads(job.options_json)
                if isinstance(loaded, dict):
                    stored = loaded
            except json.JSONDecodeError:
                stored = {}
        merged = {**stored, **(options or {})}
        return merged

    def _finalize_if_done(self, job: Job) -> None:
        counts = self._jobs.count_items_by_status(job.id)
        active = sum(counts.get(status, 0) for status in _ACTIVE_ITEM_STATUSES)
        if active > 0:
            return
        if job.status in {JobStatus.CANCELLED.value, JobStatus.PAUSED.value}:
            return
        failed = counts.get(JobItemStatus.FAILED.value, 0)
        completed = counts.get(JobItemStatus.COMPLETED.value, 0)
        job.completed_at = datetime.now(timezone.utc)
        if completed == 0 and failed > 0:
            job.status = JobStatus.FAILED.value
        else:
            job.status = JobStatus.COMPLETED.value
        self._jobs.refresh_job_counters(job)

    def _require_job(self, job_id: str) -> Job:
        job = self._jobs.get_job(job_id)
        if job is None:
            raise LookupError(f"Job not found: {job_id}")
        return job

    @staticmethod
    def options_from_create(request: JobCreateRequest) -> dict[str, Any]:
        return {
            "job_title_profile": request.job_title_profile,
            "job_titles": request.job_titles,
            "contact_email_status": request.contact_email_status,
            "max_pages": request.max_pages,
            "run_external_verification": request.run_external_verification,
            "pipeline_mode": request.pipeline_mode,
            "skip_existing_people": bool(request.skip_existing_people),
        }

    @staticmethod
    def options_from_run(request: JobRunOptions | None) -> dict[str, Any]:
        if request is None:
            return {
                "run_external_verification": False,
                "max_pages": 2,
                "per_page": 25,
                "pipeline_mode": "search_only_guess",
            }
        return request.model_dump()
