"""Configurable acceptance rules for Apollo and external verification."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings


@dataclass(frozen=True)
class AcceptanceDecision:
    """Result of evaluating one verification layer."""

    accepted: bool
    reason: str | None = None


def evaluate_apollo_email_status(
    apollo_email_status: str | None,
    *,
    required_status: str | None = None,
    settings: Settings | None = None,
) -> AcceptanceDecision:
    """
    Apollo email-status gate.

    Default required status comes from APOLLO_REQUIRED_EMAIL_STATUS (verified).
    Apollo "verified" is not a deliverability guarantee — external verification
    is a separate gate (Phase 7).
    """
    cfg = settings or get_settings()
    required = (required_status or cfg.apollo_required_email_status).strip().lower()
    if not required:
        raise ValueError("APOLLO_REQUIRED_EMAIL_STATUS is empty")

    if not apollo_email_status or not str(apollo_email_status).strip():
        return AcceptanceDecision(
            accepted=False,
            reason="missing_apollo_email_status",
        )

    actual = str(apollo_email_status).strip().lower()
    if actual != required:
        return AcceptanceDecision(
            accepted=False,
            reason=(
                f"apollo_email_status '{actual}' does not match "
                f"required '{required}'"
            ),
        )
    return AcceptanceDecision(accepted=True, reason=None)


def evaluate_external_email_status(
    external_status: str | None,
    *,
    required_status: str | None = None,
    settings: Settings | None = None,
) -> AcceptanceDecision:
    """
    External deliverability gate.

    Default required status comes from EXTERNAL_REQUIRED_EMAIL_STATUS (valid).
    """
    cfg = settings or get_settings()
    required = (required_status or cfg.external_required_email_status).strip().lower()
    if not required:
        raise ValueError("EXTERNAL_REQUIRED_EMAIL_STATUS is empty")

    if not external_status or not str(external_status).strip():
        return AcceptanceDecision(
            accepted=False,
            reason="missing_external_verification_status",
        )

    actual = str(external_status).strip().lower()
    if actual != required:
        return AcceptanceDecision(
            accepted=False,
            reason=(
                f"external_verification_status '{actual}' does not match "
                f"required '{required}'"
            ),
        )
    return AcceptanceDecision(accepted=True, reason=None)


def evaluate_final_acceptance(
    *,
    apollo_email_status: str | None,
    external_verification_status: str | None,
    settings: Settings | None = None,
) -> AcceptanceDecision:
    """
    Combined acceptance: Apollo gate AND external deliverability gate.

    Only when both pass should final_status become accepted.
    """
    cfg = settings or get_settings()
    apollo = evaluate_apollo_email_status(apollo_email_status, settings=cfg)
    if not apollo.accepted:
        return apollo
    external = evaluate_external_email_status(external_verification_status, settings=cfg)
    if not external.accepted:
        return external
    return AcceptanceDecision(accepted=True, reason=None)
