"""Shared API response envelopes."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Generic
from typing import TypeVar

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ApiResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(populate_by_name=True)

    status_code: int = Field(alias="statusCode")
    message: str | list[str]
    data: T | None = None
    error: str | object | None = None


def ok(data: T, *, status_code: int = 200, message: str | None = None) -> ApiResponse[T]:
    return ApiResponse(
        status_code=status_code,
        message=message or _default_message(status_code),
        data=data,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request, exc: HTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str | list) else str(exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "statusCode": exc.status_code,
                "message": message,
                "error": _default_error(exc.status_code),
                "data": None,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request, exc: RequestValidationError) -> JSONResponse:
        messages = [str(error["msg"]) for error in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "statusCode": 422,
                "message": messages,
                "error": "Unprocessable Entity",
                "data": None,
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled HTTP exception", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "statusCode": 500,
                "message": "Internal server error",
                "error": "InternalServerError",
                "data": None,
            },
        )


def _default_message(status_code: int) -> str:
    return "Created" if status_code == 201 else "OK"


def _default_error(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"
