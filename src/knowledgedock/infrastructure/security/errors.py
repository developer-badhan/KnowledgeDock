"""Failures that originate outside this application.

Separated from `domain.errors` because these are not business outcomes. An AI
provider being down is not the caller's fault and must not be reported as a
validation error, so it becomes a 502 rather than a 422.

`SKILL.md` §17: an external AI failure must never terminate the process, and the
client should learn nothing about why.
"""

from __future__ import annotations


class AiProviderError(RuntimeError):
    """Base for every external-AI failure. Carries a log-only `detail`."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class ProviderTimeout(AiProviderError):
    """The provider did not answer within the configured timeout."""


class ProviderUnavailable(AiProviderError):
    """Rate limited, erroring, or returning something unusable."""


class ProviderRejected(AiProviderError):
    """The request was malformed. Retrying will not help."""
