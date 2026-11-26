"""Application errors and FastAPI exception handlers."""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("conversations")


class ApiError(Exception):
    """An error that is safe and meaningful to expose through the HTTP API."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


async def validation_error_handler(
    _: Request, exc: RequestValidationError
) -> JSONResponse:
    details = []
    for item in exc.errors():
        details.append(
            {
                "location": ".".join(str(part) for part in item.get("loc", ())),
                "message": item.get("msg", "invalid value"),
            }
        )
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "details": details,
            }
        },
    )


async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "an unexpected internal error occurred",
            }
        },
    )
