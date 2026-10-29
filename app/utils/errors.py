"""Classify transient vs permanent pipeline errors for retry decisions."""

from __future__ import annotations

import httpx

from app.services.apollo import (
    ApolloAPIError,
    ApolloAuthError,
    ApolloRateLimitError,
    ApolloValidationError,
)
from app.services.email_verifier import (
    EmailVerifierAPIError,
    EmailVerifierAuthError,
    EmailVerifierRateLimitError,
)


def is_transient_error(exc: BaseException) -> bool:
    """Return True when the failure is safe to retry with backoff."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
        return True
    if isinstance(exc, ApolloRateLimitError):
        # Daily quotas won't recover in short backoff windows; fail fast.
        if (exc.window or "").lower() == "daily":
            return False
        return True
    if isinstance(exc, EmailVerifierRateLimitError):
        return True
    if isinstance(exc, ApolloAPIError) and exc.retryable:
        return True
    if isinstance(exc, EmailVerifierAPIError) and exc.retryable:
        return True
    return False


def is_permanent_error(exc: BaseException) -> bool:
    """Return True for auth/validation errors that should not be retried blindly."""
    if isinstance(exc, ApolloRateLimitError) and (exc.window or "").lower() == "daily":
        return True
    return isinstance(
        exc,
        (
            ApolloAuthError,
            ApolloValidationError,
            EmailVerifierAuthError,
            ValueError,
            LookupError,
        ),
    )


def get_retry_after_seconds(exc: BaseException) -> float | None:
    if isinstance(exc, (ApolloRateLimitError, EmailVerifierRateLimitError)):
        return exc.retry_after
    return None


def provider_for_error(exc: BaseException) -> str | None:
    if isinstance(exc, (ApolloAPIError, ApolloAuthError, ApolloRateLimitError, ApolloValidationError)):
        return "apollo"
    if isinstance(
        exc,
        (
            EmailVerifierAPIError,
            EmailVerifierAuthError,
            EmailVerifierRateLimitError,
        ),
    ):
        return "email_verifier"
    return None


def http_status_for_error(exc: BaseException) -> int | None:
    if isinstance(exc, ApolloAPIError):
        return exc.status_code
    if isinstance(exc, EmailVerifierAPIError):
        return exc.status_code
    if isinstance(exc, (ApolloRateLimitError, EmailVerifierRateLimitError)):
        return 429
    if isinstance(exc, (ApolloAuthError, EmailVerifierAuthError)):
        return 401
    return None
