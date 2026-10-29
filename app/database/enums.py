"""Shared database enums for job and contact processing states."""

from enum import StrEnum


class JobStatus(StrEnum):
    """Lifecycle states for a processing job."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CompanyStatus(StrEnum):
    """High-level company record status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class JobItemStatus(StrEnum):
    """Per-company processing states within a job."""

    PENDING = "pending"
    SEARCHING = "searching"
    ENRICHING = "enriching"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"


class FinalStatus(StrEnum):
    """Final acceptance outcome for a person/contact."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    RISKY = "risky"
    INVALID = "invalid"
    UNKNOWN = "unknown"
    DISPOSABLE = "disposable"
    CATCH_ALL = "catch_all"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class VerificationStatus(StrEnum):
    """External email verification provider outcomes."""

    VALID = "valid"
    INVALID = "invalid"
    RISKY = "risky"
    UNKNOWN = "unknown"
    DISPOSABLE = "disposable"
    CATCH_ALL = "catch_all"
    FAILED = "failed"
    UNVERIFIED = "unverified"
