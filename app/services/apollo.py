"""
Apollo.io official API client.

Verified against Apollo developer docs (July 2026):
- Auth: https://docs.apollo.io/reference/authentication
  Header: `x-api-key`
- People API Search (0 credits, no emails/phones):
  POST https://api.apollo.io/api/v1/mixed_people/api_search
  https://docs.apollo.io/reference/people-api-search
- People Enrichment (credits when data found):
  POST https://api.apollo.io/api/v1/people/match
  https://docs.apollo.io/reference/people-enrichment
- Bulk People Enrichment (max 10 people / request):
  POST https://api.apollo.io/api/v1/people/bulk_match
  https://docs.apollo.io/reference/bulk-people-enrichment
- Credits / rate limits:
  https://docs.apollo.io/docs/api-pricing
  https://docs.apollo.io/reference/rate-limits

Do not invent endpoints or parameters. Provider-specific logic stays here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.schemas.apollo import (
    ApolloBulkEnrichPersonDetail,
    ApolloBulkPeopleMatchResponse,
    ApolloEnrichPersonRequest,
    ApolloPeopleMatchResponse,
    ApolloPeopleSearchRequest,
    ApolloPeopleSearchResponse,
)
from app.utils.logging import get_logger, sanitize_headers
from app.utils.retry import RETRYABLE_STATUS_CODES, retry_async

logger = get_logger(__name__)

# Documented hard limits from Apollo reference docs.
MAX_SEARCH_DOMAINS_PER_REQUEST = 1000
MAX_SEARCH_PER_PAGE = 100
MAX_BULK_ENRICH_PEOPLE = 10

DEFAULT_BASE_URL = "https://api.apollo.io/api/v1"
AUTH_HEALTH_URL = "https://api.apollo.io/v1/auth/health"


class ApolloError(Exception):
    """Base Apollo client error."""


class ApolloAuthError(ApolloError):
    """Invalid or unauthorized API key (401/403)."""


class ApolloValidationError(ApolloError):
    """Client validation or Apollo 400/422 responses."""


class ApolloRateLimitError(ApolloError):
    """HTTP 429 (or rate-limit payload on 403) from Apollo."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        window: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        # "daily" | "hourly" | "per-minute" | None
        self.window = window


class ApolloAPIError(ApolloError):
    """Unexpected Apollo HTTP error."""

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


class ApolloClient:
    """
    Async httpx client for Apollo's documented people search/enrichment APIs.

    Notes from official docs:
    - People API Search does **not** return email or phone numbers.
    - People API Search requires a master API key.
    - Bulk enrichment accepts at most 10 people per request.
    - Rate limits are plan-specific; 429 responses must be respected.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float | None = None,
        max_retries: int | None = None,
        concurrency: int | None = None,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._api_key = (api_key if api_key is not None else cfg.apollo_api_key).strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout if timeout is not None else cfg.request_timeout
        self._max_retries = max_retries if max_retries is not None else cfg.max_retries
        self._concurrency = concurrency if concurrency is not None else cfg.apollo_concurrency
        self._owns_client = client is None
        self._client = client
        self._semaphore = asyncio.Semaphore(max(1, self._concurrency))
        self.last_raw_response: dict[str, Any] | list[Any] | None = None
        self.last_response_meta: dict[str, Any] = {}

    async def __aenter__(self) -> ApolloClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def check_auth(self) -> dict[str, Any]:
        """
        Call Apollo's auth health endpoint to validate the API key.

        Docs: GET https://api.apollo.io/v1/auth/health
        """
        response = await self._request("GET", AUTH_HEALTH_URL, use_base=False)
        return response.json()

    async def search_people(
        self,
        request: ApolloPeopleSearchRequest | None = None,
        /,
        **filters: Any,
    ) -> ApolloPeopleSearchResponse:
        """
        Search net-new people (no credits; emails/phones not returned).

        Endpoint: POST /mixed_people/api_search
        """
        payload = self._build_search_payload(request, filters)
        response = await self._request(
            "POST",
            "/mixed_people/api_search",
            json_body=payload,
        )
        raw = response.json()
        self.last_raw_response = raw if isinstance(raw, (dict, list)) else {"raw": raw}
        try:
            parsed = ApolloPeopleSearchResponse.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            meta = self.last_response_meta or {}
            logger.error(
                "apollo_people_search_parse_error",
                extra={
                    "status_code": meta.get("status_code") or response.status_code,
                    "apollo_request_id": meta.get("apollo_request_id"),
                    "path": "/mixed_people/api_search",
                    "response_headers": meta.get("response_headers"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise ApolloValidationError(
                "Failed to parse Apollo people search response "
                f"(HTTP {meta.get('status_code') or response.status_code}"
                + (
                    f", apollo_request_id={meta.get('apollo_request_id')}"
                    if meta.get("apollo_request_id")
                    else ""
                )
                + f"): {exc}"
            ) from exc
        logger.info(
            "apollo_people_search_ok",
            extra={
                "status_code": self.last_response_meta.get("status_code"),
                "apollo_request_id": self.last_response_meta.get("apollo_request_id"),
                "people": len([p for p in (parsed.people or []) if p is not None]),
                "total_entries": parsed.total_entries,
            },
        )
        return parsed

    async def enrich_person(
        self,
        request: ApolloEnrichPersonRequest | None = None,
        /,
        **fields: Any,
    ) -> ApolloPeopleMatchResponse:
        """
        Enrich a single person (may consume credits when data is found).

        Endpoint: POST /people/match
        """
        payload = (
            request.model_dump(exclude_none=True)
            if request is not None
            else ApolloEnrichPersonRequest.model_validate(fields).model_dump(
                exclude_none=True
            )
        )
        self._validate_enrich_options(payload)
        response = await self._request("POST", "/people/match", json_body=payload)
        return ApolloPeopleMatchResponse.model_validate(response.json())

    async def bulk_enrich_people(
        self,
        details: list[ApolloBulkEnrichPersonDetail | dict[str, Any]],
        *,
        reveal_personal_emails: bool = False,
        reveal_phone_number: bool = False,
        run_waterfall_email: bool = False,
        run_waterfall_phone: bool = False,
        webhook_url: str | None = None,
    ) -> ApolloBulkPeopleMatchResponse:
        """
        Enrich up to 10 people in one request (may consume credits).

        Endpoint: POST /people/bulk_match
        """
        if not details:
            raise ApolloValidationError("details must include at least one person")
        if len(details) > MAX_BULK_ENRICH_PEOPLE:
            raise ApolloValidationError(
                f"Bulk enrichment accepts at most {MAX_BULK_ENRICH_PEOPLE} people "
                f"per request (got {len(details)})"
            )

        normalized: list[dict[str, Any]] = []
        for item in details:
            if isinstance(item, ApolloBulkEnrichPersonDetail):
                normalized.append(item.model_dump(exclude_none=True))
            else:
                normalized.append(
                    ApolloBulkEnrichPersonDetail.model_validate(item).model_dump(
                        exclude_none=True
                    )
                )

        payload: dict[str, Any] = {
            "details": normalized,
            "reveal_personal_emails": reveal_personal_emails,
            "reveal_phone_number": reveal_phone_number,
            "run_waterfall_email": run_waterfall_email,
            "run_waterfall_phone": run_waterfall_phone,
        }
        if webhook_url:
            payload["webhook_url"] = webhook_url
        self._validate_enrich_options(payload)

        response = await self._request(
            "POST",
            "/people/bulk_match",
            json_body=payload,
        )
        raw = response.json()
        self.last_raw_response = raw if isinstance(raw, (dict, list)) else {"raw": raw}
        try:
            parsed = ApolloBulkPeopleMatchResponse.model_validate(raw)
        except Exception as exc:  # noqa: BLE001
            meta = self.last_response_meta or {}
            logger.error(
                "apollo_bulk_match_parse_error",
                extra={
                    "status_code": meta.get("status_code") or response.status_code,
                    "apollo_request_id": meta.get("apollo_request_id"),
                    "path": "/people/bulk_match",
                    "response_headers": meta.get("response_headers"),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "matches_len": (
                        len(raw.get("matches"))
                        if isinstance(raw, dict) and isinstance(raw.get("matches"), list)
                        else None
                    ),
                },
            )
            raise ApolloValidationError(
                "Failed to parse Apollo bulk_match response "
                f"(HTTP {meta.get('status_code') or response.status_code}"
                + (
                    f", apollo_request_id={meta.get('apollo_request_id')}"
                    if meta.get("apollo_request_id")
                    else ""
                )
                + f"): {exc}"
            ) from exc
        logger.info(
            "apollo_bulk_match_ok",
            extra={
                "status_code": self.last_response_meta.get("status_code"),
                "apollo_request_id": self.last_response_meta.get("apollo_request_id"),
                "credits_consumed": parsed.credits_consumed,
                "matches": len(parsed.matches or []),
                "missing_records": parsed.missing_records,
                "batch_size": len(normalized),
            },
        )
        return parsed

    def _build_search_payload(
        self,
        request: ApolloPeopleSearchRequest | None,
        filters: dict[str, Any],
    ) -> dict[str, Any]:
        if request is not None:
            payload = request.model_dump(exclude_none=True)
        else:
            payload = ApolloPeopleSearchRequest.model_validate(filters).model_dump(
                exclude_none=True
            )

        domains = payload.get("q_organization_domains_list") or []
        if len(domains) > MAX_SEARCH_DOMAINS_PER_REQUEST:
            raise ApolloValidationError(
                f"q_organization_domains_list accepts at most "
                f"{MAX_SEARCH_DOMAINS_PER_REQUEST} domains per request"
            )

        per_page = payload.get("per_page")
        if per_page is not None and (
            per_page < 1 or per_page > MAX_SEARCH_PER_PAGE
        ):
            raise ApolloValidationError(
                f"per_page must be between 1 and {MAX_SEARCH_PER_PAGE}"
            )

        return payload

    @staticmethod
    def _validate_enrich_options(payload: dict[str, Any]) -> None:
        needs_webhook = bool(
            payload.get("reveal_phone_number")
            or payload.get("run_waterfall_email")
            or payload.get("run_waterfall_phone")
        )
        if needs_webhook and not payload.get("webhook_url"):
            raise ApolloValidationError(
                "webhook_url is required when reveal_phone_number or waterfall "
                "enrichment flags are enabled (Apollo docs)"
            )

    async def _ensure_client(self) -> httpx.AsyncClient:
        if not self._api_key:
            raise ApolloAuthError(
                "APOLLO_API_KEY is not configured. Set it in the environment."
            )
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers=self._default_headers(),
            )
        return self._client

    def _default_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "accept": "application/json",
            "x-api-key": self._api_key,
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        use_base: bool = True,
    ) -> httpx.Response:
        del use_base  # Absolute paths are passed explicitly by callers when needed.
        client = await self._ensure_client()

        async def _send() -> httpx.Response:
            async with self._semaphore:
                response = await client.request(
                    method,
                    path,
                    json=json_body,
                    headers=self._default_headers(),
                )
            return self._handle_response(response, method=method, path=path)


        def should_retry(exc: BaseException) -> bool:
            if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
                return True
            if isinstance(exc, ApolloRateLimitError):
                # Daily quotas won't recover during short client retries.
                if (exc.window or "").lower() == "daily":
                    return False
                return True
            if isinstance(exc, ApolloAPIError):
                return exc.retryable
            return False

        def get_retry_after(exc: BaseException) -> float | None:
            if isinstance(exc, ApolloRateLimitError):
                return exc.retry_after
            return None

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            logger.warning(
                "apollo_retry",
                extra={
                    "attempt": attempt,
                    "delay_seconds": delay,
                    "method": method,
                    "path": path,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )

        return await retry_async(
            _send,
            max_retries=self._max_retries,
            should_retry=should_retry,
            get_retry_after=get_retry_after,
            on_retry=on_retry,
        )

    def _handle_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
    ) -> httpx.Response:
        status = response.status_code
        response_headers = sanitize_headers(dict(response.headers))
        request_id = self.extract_apollo_request_id(response.headers)
        self.last_response_meta = {
            "status_code": status,
            "method": method,
            "path": path,
            "apollo_request_id": request_id,
            "response_headers": response_headers,
        }
        logger.info(
            "apollo_response",
            extra={
                "status_code": status,
                "method": method,
                "path": path,
                "apollo_request_id": request_id,
                "response_headers": response_headers,
            },
        )

        if status == 401 or status == 403:
            detail = self._safe_error_body(response)
            if status == 403 and self._looks_like_rate_limit(detail, response):
                retry_after = self._parse_retry_after(response)
                window = self._rate_limit_window_label(detail)
                message = self._rate_limit_message(
                    path=path,
                    request_id=request_id,
                    detail=detail,
                    status_code=status,
                    window=window,
                )
                logger.warning(
                    "apollo_rate_limited",
                    extra={
                        "status_code": status,
                        "method": method,
                        "path": path,
                        "apollo_request_id": request_id,
                        "retry_after": retry_after,
                        "window": window,
                        "detail": detail[:300],
                    },
                )
                raise ApolloRateLimitError(
                    message, retry_after=retry_after, window=window
                )

            logger.error(
                "apollo_auth_error",
                extra={
                    "status_code": status,
                    "method": method,
                    "path": path,
                    "apollo_request_id": request_id,
                    "response_headers": response_headers,
                    "detail": detail[:300],
                },
            )
            raise ApolloAuthError(
                f"Apollo authentication failed ({status})"
                + (f" [apollo_request_id={request_id}]" if request_id else "")
                + ". Check APOLLO_API_KEY and key scopes "
                "(People API Search requires a master key)."
                + (f" Details: {detail}" if detail else "")
            )

        if status == 429:
            detail = self._safe_error_body(response)
            retry_after = self._parse_retry_after(response)
            window = self._rate_limit_window_label(detail)
            message = self._rate_limit_message(
                path=path,
                request_id=request_id,
                detail=detail,
                status_code=status,
                window=window,
            )
            logger.warning(
                "apollo_rate_limited",
                extra={
                    "status_code": status,
                    "method": method,
                    "path": path,
                    "apollo_request_id": request_id,
                    "retry_after": retry_after,
                    "window": window,
                    "detail": detail[:300],
                },
            )
            raise ApolloRateLimitError(
                message, retry_after=retry_after, window=window
            )

        if status in {400, 422}:
            detail = self._safe_error_body(response)
            raise ApolloValidationError(
                f"Apollo validation error ({status}) for {path}"
                + (f" [apollo_request_id={request_id}]" if request_id else "")
                + f": {detail}"
            )

        if status in RETRYABLE_STATUS_CODES:
            detail = self._safe_error_body(response)
            raise ApolloAPIError(
                f"Apollo transient error ({status}) for {path}"
                + (f" [apollo_request_id={request_id}]" if request_id else "")
                + f": {detail}",
                status_code=status,
                retryable=True,
            )

        if status >= 400:
            detail = self._safe_error_body(response)
            logger.error(
                "apollo_http_error",
                extra={
                    "status_code": status,
                    "method": method,
                    "path": path,
                    "apollo_request_id": request_id,
                    "detail": detail,
                },
            )
            raise ApolloAPIError(
                f"Apollo request failed ({status}) for {path}"
                + (f" [apollo_request_id={request_id}]" if request_id else "")
                + f": {detail}",
                status_code=status,
                retryable=False,
            )

        return response

    @staticmethod
    def extract_apollo_request_id(headers: Any) -> str | None:
        """Pull Apollo/cloud request id from common response header names."""
        if headers is None:
            return None
        candidates = (
            "x-request-id",
            "x-apollo-request-id",
            "apollo-request-id",
            "request-id",
            "x-amzn-requestid",
            "x-amzn-trace-id",
            "cf-ray",
        )
        for name in candidates:
            try:
                value = headers.get(name)
            except Exception:  # noqa: BLE001
                value = None
            if value:
                text = str(value).strip()
                if text:
                    return text
        return None

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _looks_like_rate_limit(detail: str, response: httpx.Response) -> bool:
        """Detect Apollo rate-limit payloads that sometimes arrive as HTTP 403."""
        text = (detail or "").lower()
        markers = (
            "rate limit",
            "rate_limit",
            "too many requests",
            "api calls allowed",
            "api_rate_limit",
            "usage.rate_limit",
            "more api requests than your plan",
            "times per day",
            "times per hour",
            "times per minute",
        )
        if any(marker in text for marker in markers):
            return True
        # Nested JSON message strings occasionally appear escaped in the body.
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(payload, dict):
            return False
        blob = str(payload).lower()
        return any(marker in blob for marker in markers)

    @classmethod
    def _rate_limit_message(
        cls,
        *,
        path: str,
        request_id: str | None,
        detail: str,
        status_code: int,
        window: str | None = None,
    ) -> str:
        window = window or cls._rate_limit_window_label(detail)
        base = (
            f"Apollo API rate limit exceeded for {path} ({status_code})"
            + (f" — {window} quota" if window else "")
            + ". Stop new runs and wait for the quota to reset "
            "(or upgrade your Apollo plan). "
            "Retrying now will keep burning the same limit."
        )
        if request_id:
            base += f" [apollo_request_id={request_id}]"
        if detail:
            base += f" Apollo says: {detail[:400]}"
        return base

    @staticmethod
    def _rate_limit_window_label(detail: str) -> str | None:
        text = (detail or "").lower()
        if "per day" in text or "times per day" in text:
            return "daily"
        if "per hour" in text or "times per hour" in text:
            return "hourly"
        if "per minute" in text or "times per minute" in text:
            return "per-minute"
        return None

    @staticmethod
    def _safe_error_body(response: httpx.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                for key in ("message", "error", "error_message", "detail"):
                    if key in payload:
                        return str(payload[key])[:500]
            return str(payload)[:500]
        except Exception:  # noqa: BLE001
            return (response.text or "")[:500]
