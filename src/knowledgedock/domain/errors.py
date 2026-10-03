"""Typed application errors.

`SKILL.md` §23 requires predictable errors and forbids leaking stack traces to
clients. Each error carries the HTTP status it maps to and a stable machine code,
so a client can branch on `code` without parsing prose, and so logs can record the
diagnostic detail that the response deliberately withholds.

Routes never raise `HTTPException` for business rules; they let these propagate to
the handlers registered in `app.py`, which keeps status-code decisions in one place.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    VALIDATION_FAILED = "validation_failed"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    PROVIDER_ERROR = "provider_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """Base class for every error the application raises on purpose."""

    status_code = 500
    code = ErrorCode.INTERNAL_ERROR
    default_message = "Something went wrong."

    def __init__(self, message: str | None = None, *, detail: str | None = None) -> None:
        self.message = message or self.default_message
        # `detail` is for the log only. It must never reach the response body.
        self.detail = detail
        super().__init__(self.message)


class ValidationFailed(AppError):
    status_code = 422
    code = ErrorCode.VALIDATION_FAILED
    default_message = "The submitted data is not valid."


class AuthenticationFailed(AppError):
    """Deliberately vague: the same message and status for every failure mode.

    Distinguishing 'no such user' from 'wrong password' turns the login endpoint
    into an account-enumeration oracle.
    """

    status_code = 401
    code = ErrorCode.AUTHENTICATION_FAILED
    default_message = "Incorrect email or password."


class PermissionDenied(AppError):
    status_code = 403
    code = ErrorCode.PERMISSION_DENIED
    default_message = "You do not have access to this resource."


class NotFound(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND
    default_message = "The requested resource does not exist."


class Conflict(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT
    default_message = "That resource already exists."


class RateLimited(AppError):
    status_code = 429
    code = ErrorCode.RATE_LIMITED
    default_message = "Too many requests. Please slow down."


class ProviderError(AppError):
    """An external dependency failed. The client learns nothing about why."""

    status_code = 502
    code = ErrorCode.PROVIDER_ERROR
    default_message = "An upstream service is unavailable. Please try again."


class ServiceUnavailable(AppError):
    """A dependency this application owns is not reachable right now.

    Distinct from `ProviderError`: 502 blames something upstream, while 503 says
    "this service cannot serve that right now", which is what an unreachable
    database actually means to a client.
    """

    status_code = 503
    code = ErrorCode.SERVICE_UNAVAILABLE
    default_message = "The service is temporarily unavailable. Please try again."
