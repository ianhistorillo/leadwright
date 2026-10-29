"""Company import and listing endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.config import get_settings
from app.database.session import get_db
from app.schemas.company import CompanyImportSummary, CompanyListResponse, CompanyRead
from app.services.company_service import CompanyService

router = APIRouter(
    prefix="/companies",
    tags=["companies"],
    dependencies=[Depends(require_api_key)],
)


@router.post(
    "/import",
    response_model=CompanyImportSummary,
    status_code=status.HTTP_201_CREATED,
)
async def import_companies(
    file: UploadFile = File(
        ...,
        description="CSV with company_name and/or company_website (optional company_domain)",
    ),
    db: Session = Depends(get_db),
) -> CompanyImportSummary:
    """Import companies from a CSV upload, normalize domains, and deduplicate."""
    settings = get_settings()
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only .csv uploads are allowed",
        )

    content_type = (file.content_type or "").lower()
    allowed_types = {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
        "",
    }
    if content_type and content_type not in allowed_types and "csv" not in content_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported content type: {file.content_type}",
        )

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    content = await file.read()
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum upload size of {settings.max_upload_size_mb} MB",
        )

    service = CompanyService(db)
    try:
        return service.import_csv_bytes(content, filename=filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get("", response_model=CompanyListResponse)
def list_companies(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> CompanyListResponse:
    """List imported companies."""
    return CompanyService(db).list_companies(offset=offset, limit=limit)


@router.get("/{company_id}", response_model=CompanyRead)
def get_company(company_id: str, db: Session = Depends(get_db)) -> CompanyRead:
    """Fetch a single company by ID."""
    company = CompanyService(db).get_company(company_id)
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )
    return CompanyRead.model_validate(company)
