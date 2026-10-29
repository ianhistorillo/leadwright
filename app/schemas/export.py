"""Export schemas for job CSV artifacts."""

from __future__ import annotations

from pydantic import BaseModel, Field


class JobExportResponse(BaseModel):
    """Metadata returned after generating export CSVs for a job."""

    job_id: str
    output_dir: str
    accepted_contacts_path: str
    rejected_contacts_path: str
    processing_errors_path: str
    accepted_count: int
    rejected_count: int
    processing_errors_count: int
    files: list[str] = Field(default_factory=list)
