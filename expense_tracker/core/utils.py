from http import HTTPStatus
from typing import Any

from fastapi.responses import JSONResponse


def error_body(code: str, message: str, details: list[Any] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or []}}


def error_response(
    status_code: int,
    message: str,
    details: list[Any] | None = None,
    headers: dict | None = None,
    code: str | None = None,
) -> JSONResponse:
    """The one error format for the whole API. `code` defaults to the status name, e.g. 404 -> "not_found"."""
    code = code or HTTPStatus(status_code).phrase.lower().replace(" ", "_")
    return JSONResponse(error_body(code, message, details), status_code=status_code, headers=headers)
