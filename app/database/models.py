"""ORM models for companies, people, verifications, and jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base
from app.database.enums import (
    CompanyStatus,
    FinalStatus,
    JobItemStatus,
    JobStatus,
    VerificationStatus,
)


def _uuid() -> str:
    return str(uuid4())


class Company(Base):
    """A company discovered or imported from a website/domain."""

    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_companies_domain"),
        Index("ix_companies_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Official/display name — filled from Apollo Search organization.name only.
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # CSV company_name used only as a search hint when website/domain is missing.
    lookup_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    website: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    apollo_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=CompanyStatus.PENDING.value,
        server_default=CompanyStatus.PENDING.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    people: Mapped[list[Person]] = relationship(back_populates="company")
    job_items: Mapped[list[JobItem]] = relationship(back_populates="company")


class Person(Base):
    """An employee/contact associated with a company."""

    __tablename__ = "people"
    __table_args__ = (
        # Not UNIQUE: search-only email guesses often collide (firstname@domain).
        # Enrichment still de-dupes in application code before writing this column.
        UniqueConstraint("apollo_person_id", name="uq_people_apollo_person_id"),
        Index("ix_people_company_id", "company_id"),
        Index("ix_people_normalized_email", "normalized_email"),
        Index("ix_people_final_status", "final_status"),
        Index("ix_people_apollo_email_status", "apollo_email_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    apollo_person_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    # Job that first created this person — used by "new people only" batch exports.
    source_job_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    job_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    linkedin_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    normalized_email: Mapped[Optional[str]] = mapped_column(String(320), nullable=True)
    apollo_email_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    final_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=FinalStatus.UNVERIFIED.value,
        server_default=FinalStatus.UNVERIFIED.value,
    )
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    company: Mapped[Company] = relationship(back_populates="people")
    email_verifications: Mapped[list[EmailVerification]] = relationship(
        back_populates="person",
        cascade="all, delete-orphan",
    )


class EmailVerification(Base):
    """External email verification result for a person."""

    __tablename__ = "email_verifications"
    __table_args__ = (
        Index("ix_email_verifications_person_id", "person_id"),
        Index("ix_email_verifications_status", "status"),
        Index("ix_email_verifications_email", "email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    person_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("people.id", ondelete="CASCADE"),
        nullable=False,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=VerificationStatus.UNVERIFIED.value,
        server_default=VerificationStatus.UNVERIFIED.value,
    )
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    raw_response: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Optional sanitized provider payload; never store API keys.",
    )

    person: Mapped[Person] = relationship(back_populates="email_verifications")


class Job(Base):
    """A batch processing job over imported companies."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobStatus.PENDING.value,
        server_default=JobStatus.PENDING.value,
    )
    total_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    successful_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_companies: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    options_json: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        doc="Serialized run options used for resume/retry continuity.",
    )

    items: Mapped[list[JobItem]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )
    processing_errors: Mapped[list[ProcessingError]] = relationship(
        "ProcessingError",
        back_populates="job",
        cascade="all, delete-orphan",
    )


class JobItem(Base):
    """Tracks processing of a single company within a job."""

    __tablename__ = "job_items"
    __table_args__ = (
        UniqueConstraint("job_id", "company_id", name="uq_job_items_job_company"),
        Index("ix_job_items_status", "status"),
        Index("ix_job_items_job_id", "job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    company_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=JobItemStatus.PENDING.value,
        server_default=JobItemStatus.PENDING.value,
    )
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    job: Mapped[Job] = relationship(back_populates="items")
    company: Mapped[Company] = relationship(back_populates="job_items")


class ProcessingError(Base):
    """Tracked failed API / pipeline request for observability and retry analysis."""

    __tablename__ = "processing_errors"
    __table_args__ = (
        Index("ix_processing_errors_job_id", "job_id"),
        Index("ix_processing_errors_job_item_id", "job_item_id"),
        Index("ix_processing_errors_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=True,
    )
    job_item_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("job_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    company_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    job: Mapped[Optional[Job]] = relationship(back_populates="processing_errors")
