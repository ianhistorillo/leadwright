"""HTTP middleware for request IDs, logging, and upload size limits."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a request ID and emit structured access logs (no secrets)."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response: Response
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response


class LimitUploadSizeMiddleware(BaseHTTPMiddleware):
    """Reject oversized request bodies early using Content-Length when present."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        settings = get_settings()
        max_bytes = settings.max_upload_size_mb * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                length = int(content_length)
            except ValueError:
                length = 0
            if length > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Request body exceeds maximum upload size of "
                            f"{settings.max_upload_size_mb} MB"
                        ),
                        "error_code": "http_413",
                        "request_id": getattr(request.state, "request_id", None),
                        "errors": None,
                    },
                )
        return await call_next(request)
