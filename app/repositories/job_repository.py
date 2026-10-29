"""Job and job-item persistence helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database.enums import JobItemStatus, JobStatus
from app.database.models import Job, JobItem


class JobRepository:
    """Data-access layer for Job / JobItem records."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_job(self, job_id: str) -> Job | None:
        return self._session.get(Job, job_id)

    def get_job_with_items(self, job_id: str) -> Job | None:
        stmt = (
            select(Job)
            .where(Job.id == job_id)
            .options(selectinload(Job.items))
        )
        return self._session.scalar(stmt)

    def list_jobs(self, *, offset: int = 0, limit: int = 100) -> list[Job]:
        stmt = (
            select(Job)
            .order_by(Job.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(self._session.scalars(stmt).all())

    def count_jobs(self) -> int:
        return int(self._session.scalar(select(func.count()).select_from(Job)) or 0)

    def create_job(self, *, name: str, total_companies: int = 0) -> Job:
        job = Job(
            name=name,
            status=JobStatus.PENDING.value,
            total_companies=total_companies,
            processed_companies=0,
            successful_companies=0,
            failed_companies=0,
        )
        self._session.add(job)
        return job

    def create_job_item(self, *, job_id: str, company_id: str) -> JobItem:
        item = JobItem(
            job_id=job_id,
            company_id=company_id,
            status=JobItemStatus.PENDING.value,
            retry_count=0,
        )
        self._session.add(item)
        return item

    def list_items(
        self,
        job_id: str,
        *,
        statuses: list[str] | None = None,
        limit: int | None = None,
    ) -> list[JobItem]:
        stmt = select(JobItem).where(JobItem.job_id == job_id)
        if statuses:
            stmt = stmt.where(JobItem.status.in_(statuses))
        stmt = stmt.order_by(JobItem.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt).all())

    def count_items_by_status(self, job_id: str) -> dict[str, int]:
        stmt = (
            select(JobItem.status, func.count())
            .where(JobItem.job_id == job_id)
            .group_by(JobItem.status)
        )
        return {status: int(count) for status, count in self._session.execute(stmt).all()}

    def mark_item(
        self,
        item: JobItem,
        *,
        status: str,
        error_message: str | None = None,
        increment_retry: bool = False,
    ) -> None:
        item.status = status
        if error_message is not None:
            item.error_message = error_message
        if increment_retry:
            item.retry_count += 1
        if status in {JobItemStatus.COMPLETED.value, JobItemStatus.FAILED.value}:
            item.processed_at = datetime.now(timezone.utc)

    def refresh_job_counters(self, job: Job) -> None:
        counts = self.count_items_by_status(job.id)
        completed = counts.get(JobItemStatus.COMPLETED.value, 0)
        failed = counts.get(JobItemStatus.FAILED.value, 0)
        job.successful_companies = completed
        job.failed_companies = failed
        job.processed_companies = completed + failed
