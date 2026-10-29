"""Job management API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.database.session import get_db
from app.schemas.job import (
    JobCreateRequest,
    JobListResponse,
    JobProgressResponse,
    JobRead,
    JobResultsResponse,
    JobRunOptions,
    ProcessingErrorRead,
)
from app.services.job_service import JobService, JobStateError

router = APIRouter(
    prefix="/jobs",
    tags=["jobs"],
    dependencies=[Depends(require_api_key)],
)


def _map_job_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, JobStateError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unexpected job service error",
    )


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreateRequest, db: Session = Depends(get_db)) -> JobRead:
    """Create a job and job items for selected (or all) companies."""
    try:
        return JobService(db).create_job(payload)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.get("", response_model=JobListResponse)
def list_jobs(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> JobListResponse:
    return JobService(db).list_jobs(offset=offset, limit=limit)


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, db: Session = Depends(get_db)) -> JobRead:
    job = JobService(db).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobRead.model_validate(job)


@router.post("/{job_id}/start", response_model=JobRead)
def start_job(
    job_id: str,
    payload: JobRunOptions | None = None,
    db: Session = Depends(get_db),
) -> JobRead:
    """Enqueue Celery processing for a pending/paused job."""
    try:
        options = JobService.options_from_run(payload)
        return JobService(db).start_job(job_id, options=options)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.post("/{job_id}/pause", response_model=JobRead)
def pause_job(job_id: str, db: Session = Depends(get_db)) -> JobRead:
    try:
        return JobService(db).pause_job(job_id)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.post("/{job_id}/resume", response_model=JobRead)
def resume_job(
    job_id: str,
    payload: JobRunOptions | None = None,
    db: Session = Depends(get_db),
) -> JobRead:
    try:
        options = JobService.options_from_run(payload)
        return JobService(db).resume_job(job_id, options=options)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.post("/{job_id}/cancel", response_model=JobRead)
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> JobRead:
    try:
        return JobService(db).cancel_job(job_id)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.post("/{job_id}/retry-failed", response_model=JobRead)
def retry_failed(
    job_id: str,
    payload: JobRunOptions | None = None,
    db: Session = Depends(get_db),
) -> JobRead:
    try:
        options = JobService.options_from_run(payload)
        return JobService(db).retry_failed(job_id, options=options)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.get("/{job_id}/progress", response_model=JobProgressResponse)
def job_progress(job_id: str, db: Session = Depends(get_db)) -> JobProgressResponse:
    try:
        return JobService(db).get_progress(job_id)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.get("/{job_id}/results", response_model=JobResultsResponse)
def job_results(job_id: str, db: Session = Depends(get_db)) -> JobResultsResponse:
    try:
        return JobService(db).get_results(job_id)
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc


@router.get("/{job_id}/errors", response_model=list[ProcessingErrorRead])
def job_errors(
    job_id: str,
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[ProcessingErrorRead]:
    """List tracked processing/API failures for a job."""
    try:
        rows = JobService(db).list_processing_errors(job_id, limit=limit)
        return [ProcessingErrorRead.model_validate(row) for row in rows]
    except Exception as exc:
        mapped = _map_job_errors(exc)
        if mapped.status_code == 500:
            raise
        raise mapped from exc
