"""Exception types. Callers can branch on ``code`` and ``retryable``."""

from __future__ import annotations


class GreenlineError(Exception):
    """Base class for every error raised by Greenline."""

    code: str = "greenline_error"
    retryable: bool = False


class NotConfigured(GreenlineError):
    """A required setting (secret, token, provider) is missing."""

    code = "not_configured"


class NumberNotReady(GreenlineError):
    """No live sending number is registered for the tenant (and no shared one either)."""

    code = "number_not_ready"


class ProviderError(GreenlineError):
    """The messaging provider refused or failed a call."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        retryable: bool = False,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


class RateLimited(ProviderError):
    """The provider asked us to slow down. ``retry_after`` is in seconds when known."""

    def __init__(
        self,
        message: str = "rate limited",
        *,
        retry_after: float | None = None,
        http_status: int | None = 429,
    ) -> None:
        super().__init__(message, code="rate_limited", retryable=True, http_status=http_status)
        self.retry_after = retry_after
