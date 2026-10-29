"""CSV export endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.database.session import get_db
from app.schemas.export import JobExportResponse
from app.services.export_service import ExportService

router = APIRouter(tags=["exports"], dependencies=[Depends(require_api_key)])


@router.get("/jobs/{job_id}/export", response_model=JobExportResponse)
def export_job(job_id: str, db: Session = Depends(get_db)) -> JobExportResponse:
    """
    Generate accepted_contacts.csv, rejected_contacts.csv, and processing_errors.csv.

    Files are written under data/output/{job_id}/.
    """
    try:
        return ExportService(db).export_job(job_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/export/{filename}")
def download_export_file(
    job_id: str,
    filename: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    """Download a previously generated export CSV."""
    service = ExportService(db)
    try:
        path = service.resolve_export_file(job_id, filename)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return FileResponse(
        path=path,
        media_type="text/csv",
        filename=filename,
    )
