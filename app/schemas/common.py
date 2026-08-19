"""Response shapes shared across endpoints."""

from typing import Any

from pydantic import BaseModel, Field


class Message(BaseModel):
    message: str


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str
    type: str | None = None


class ErrorBody(BaseModel):
    code: str = Field(examples=["not_found"])
    message: str
    details: list[ErrorDetail] = []


class ErrorResponse(BaseModel):
    """The single envelope every failed request returns."""

    error: ErrorBody
    request_id: str | None = None


class HealthStatus(BaseModel):
    status: str = Field(examples=["ok"])
    version: str
    environment: str
    database: str = Field(examples=["connected"])
    details: dict[str, Any] = {}


class CurrencyList(BaseModel):
    """Reference data: which currency codes the API will accept."""

    default: str = Field(examples=["USD"])
    codes: list[str]
