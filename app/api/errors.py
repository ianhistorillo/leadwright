"""Shared API error models and exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.utils.logging import get_logger

logger = get_logger(__name__)


class ErrorResponse(BaseModel):
    """Consistent API error payload."""

    detail: str
    error_code: str = "error"
    request_id: str | None = None
    errors: list[dict[str, Any]] | None = Field(
        default=None,
        description="Optional field-level validation errors",
    )


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach global exception handlers for consistent JSON errors."""

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        payload = ErrorResponse(
            detail=detail,
            error_code=f"http_{exc.status_code}",
            request_id=_request_id(request),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        payload = ErrorResponse(
            detail="Request validation failed",
            error_code="validation_error",
            request_id=_request_id(request),
            errors=[
                {
                    "loc": [str(part) for part in err.get("loc", ())],
                    "msg": err.get("msg"),
                    "type": err.get("type"),
                }
                for err in exc.errors()
            ],
        )
        return JSONResponse(
            status_code=422,
            content=payload.model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.exception(
            "unhandled_api_error",
            extra={
                "path": request.url.path,
                "method": request.method,
                "request_id": _request_id(request),
                "error_type": type(exc).__name__,
            },
        )
        payload = ErrorResponse(
            detail="Internal server error",
            error_code="internal_error",
            request_id=_request_id(request),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=payload.model_dump(),
        )
