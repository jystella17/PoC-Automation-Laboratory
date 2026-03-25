from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class NotFoundError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _error_body(status: int, error: str, message: str, path: str, details=None) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "error": error,
        "message": message,
        "path": path,
        "details": details,
    }


async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content=_error_body(404, "Not Found", exc.message, str(request.url.path)),
    )


async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    field_errors = [
        {
            "field": ".".join(str(loc) for loc in e["loc"][1:]),
            "message": e["msg"],
            "rejectedValue": e.get("input"),
        }
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=_error_body(
            422,
            "Unprocessable Entity",
            "Validation failed",
            str(request.url.path),
            {"fieldErrors": field_errors},
        ),
    )


async def unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_error_body(500, "Internal Server Error", "Internal server error", str(request.url.path)),
    )
