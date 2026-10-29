"""
External email verification provider abstraction.

Initial provider: ZeroBounce API v2
Docs:
- https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-validate-emails
- https://www.zerobounce.net/docs/email-validation-api-quickstart/v2-status-codes

Endpoint: GET https://api.zerobounce.net/v2/validate
Auth: api_key query parameter (never log the key)
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings, get_settings
from app.database.enums import VerificationStatus
from app.schemas.verification import VerificationResult
from app.utils.logging import get_logger, redact_secrets
from app.utils.retry import RETRYABLE_STATUS_CODES, retry_async

logger = get_logger(__name__)

ZEROBOUNCE_VALIDATE_URL = "https://api.zerobounce.net/v2/validate"

# Official ZeroBounce status values → internal VerificationStatus
ZEROBOUNCE_STATUS_MAP: dict[str, str] = {
    "valid": VerificationStatus.VALID.value,
    "invalid": VerificationStatus.INVALID.value,
    "catch-all": VerificationStatus.CATCH_ALL.value,
    "unknown": VerificationStatus.UNKNOWN.value,
    "spamtrap": VerificationStatus.DISPOSABLE.value,
    "abuse": VerificationStatus.RISKY.value,
    "do_not_mail": VerificationStatus.RISKY.value,
}


class EmailVerifierError(Exception):
    """Base email verifier error."""


class EmailVerifierAuthError(EmailVerifierError):
    """Invalid API key / unauthorized."""


class EmailVerifierRateLimitError(EmailVerifierError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class EmailVerifierAPIError(EmailVerifierError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class EmailVerifier(ABC):
    """Provider-agnostic email verification interface."""

    provider_name: str

    @abstractmethod
    async def verify(self, email: str) -> VerificationResult:
        """Verify a single email address."""

    async def aclose(self) -> None:
        """Close any underlying HTTP resources."""


class ZeroBounceEmailVerifier(EmailVerifier):
    """ZeroBounce v2 single-email validator."""

    provider_name = "zerobounce"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        concurrency: int | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        base_url: str = ZEROBOUNCE_VALIDATE_URL,
    ) -> None:
        cfg = settings or get_settings()
        self._api_key = (api_key if api_key is not None else cfg.email_verifier_api_key).strip()
        self._timeout = timeout if timeout is not None else cfg.request_timeout
        self._max_retries = max_retries if max_retries is not None else cfg.max_retries
        self._concurrency = concurrency if concurrency is not None else cfg.email_verifier_concurrency
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client
        self._semaphore = asyncio.Semaphore(max(1, self._concurrency))

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def verify(self, email: str) -> VerificationResult:
        if not email or "@" not in email:
            raise EmailVerifierError(f"Invalid email for verification: {email!r}")
        if not self._api_key:
            raise EmailVerifierAuthError(
                "EMAIL_VERIFIER_API_KEY is not configured. Set it in the environment."
            )

        client = await self._ensure_client()

        async def _send() -> httpx.Response:
            # Official docs use query params; ip_address may be empty.
            params = {
                "api_key": self._api_key,
                "email": email,
                "ip_address": "",
            }
            async with self._semaphore:
                return await client.get(self._base_url, params=params)

        def should_retry(exc: BaseException) -> bool:
            if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
                return True
            if isinstance(exc, EmailVerifierRateLimitError):
                return True
            if isinstance(exc, EmailVerifierAPIError):
                return exc.retryable
            return False

        def get_retry_after(exc: BaseException) -> float | None:
            if isinstance(exc, EmailVerifierRateLimitError):
                return exc.retry_after
            return None

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "email_verifier_retry",
                extra={
                    "provider": self.provider_name,
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    # Never include the request URL (contains api_key).
                },
            )

        response = await retry_async(
            _send,
            max_retries=self._max_retries,
            should_retry=should_retry,
            get_retry_after=get_retry_after,
            on_retry=on_retry,
        )
        return self._parse_response(email, response)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _parse_response(self, email: str, response: httpx.Response) -> VerificationResult:
        status_code = response.status_code
        if status_code == 401 or status_code == 403:
            raise EmailVerifierAuthError(
                f"ZeroBounce authentication failed ({status_code})"
            )
        if status_code == 429:
            retry_after = None
            raw = response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except ValueError:
                    retry_after = None
            raise EmailVerifierRateLimitError(
                "ZeroBounce rate limit exceeded",
                retry_after=retry_after,
            )
        if status_code in RETRYABLE_STATUS_CODES:
            raise EmailVerifierAPIError(
                f"ZeroBounce transient error ({status_code})",
                status_code=status_code,
                retryable=True,
            )
        if status_code >= 400:
            raise EmailVerifierAPIError(
                f"ZeroBounce request failed ({status_code})",
                status_code=status_code,
                retryable=False,
            )

        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise EmailVerifierAPIError("ZeroBounce returned non-JSON response") from exc

        if not isinstance(payload, dict):
            raise EmailVerifierAPIError("ZeroBounce returned unexpected payload type")

        if "error" in payload:
            message = str(payload.get("error"))
            lower = message.lower()
            if "api key" in lower or "credits" in lower:
                raise EmailVerifierAuthError(message)
            raise EmailVerifierAPIError(message, retryable=False)

        zb_status = str(payload.get("status") or "").strip().lower()
        sub_status = str(payload.get("sub_status") or "").strip() or None
        mapped = ZEROBOUNCE_STATUS_MAP.get(zb_status, VerificationStatus.UNKNOWN.value)

        # Safe raw payload: drop any accidental credential-looking keys.
        safe_raw = redact_secrets(payload)

        return VerificationResult(
            email=str(payload.get("address") or email),
            provider=self.provider_name,
            status=mapped,
            reason=sub_status or zb_status or None,
            raw=safe_raw if isinstance(safe_raw, dict) else None,
        )


def create_email_verifier(
    settings: Settings | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> EmailVerifier:
    """Factory for the configured email verification provider."""
    cfg = settings or get_settings()
    provider = (cfg.email_verifier_provider or "zerobounce").strip().lower()
    if provider in {"zerobounce", "zero_bounce", "zb"}:
        return ZeroBounceEmailVerifier(settings=cfg, client=client)
    raise ValueError(
        f"Unsupported EMAIL_VERIFIER_PROVIDER={provider!r}. "
        "Supported: zerobounce"
    )


def safe_raw_response_json(raw: dict[str, Any] | None) -> str | None:
    """Serialize a sanitized provider payload for DB storage."""
    if not raw:
        return None
    return json.dumps(redact_secrets(raw), separators=(",", ":"), sort_keys=True)
