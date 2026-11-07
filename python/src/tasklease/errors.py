"""Controlled client, service and configuration failures."""

from __future__ import annotations

from typing import ClassVar


class TaskleaseError(Exception):
    """Base class for tasklease failures that callers can handle."""


class APIError(TaskleaseError):
    """A refused operation with a code from the wire protocol."""

    code: ClassVar[str]
    status_code: ClassVar[int]


class Invalid(APIError):
    code = "invalid"
    status_code = 400


class Unauthenticated(APIError):
    code = "unauthenticated"
    status_code = 401


class Forbidden(APIError):
    code = "forbidden"
    status_code = 403


class NotFound(APIError):
    code = "not_found"
    status_code = 404


class Empty(APIError):
    code = "empty"
    status_code = 404


class Exists(APIError):
    code = "exists"
    status_code = 409


class TooLarge(APIError):
    code = "too_large"
    status_code = 413


class TransportError(TaskleaseError):
    """An unavailable service or uncertain network outcome.

    A mutating request may already have taken effect. The caller must decide
    whether a retry is appropriate for that operation and its live task ID.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProtocolError(TaskleaseError):
    """The service did not return a recognizable protocol envelope."""


class ConfigError(TaskleaseError, ValueError):
    """Configuration is missing, unreadable or invalid."""


API_ERRORS: dict[str, type[APIError]] = {
    error.code: error
    for error in (
        Invalid,
        Unauthenticated,
        Forbidden,
        NotFound,
        Empty,
        Exists,
        TooLarge,
    )
}
