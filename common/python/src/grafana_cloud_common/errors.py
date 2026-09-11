"""Error taxonomy.

The split that matters in a Lambda is retryable vs permanent. A permanent error
raised back to Lambda is retried by Lambda anyway, then lands in the DLQ, then
gets retried again on redrive - so a bad label set or an unparseable object can
burn an entire concurrency budget on work that can never succeed. Classify at the
point of failure and let the handler decide.
"""

from __future__ import annotations


class GrafanaCloudError(Exception):
    """Base class for everything this library raises."""


class ConfigError(GrafanaCloudError):
    """Configuration is missing or invalid.

    Permanent by definition: raised during cold start, before any work is done.
    """


class RetryableError(GrafanaCloudError):
    """The operation may succeed if tried again.

    Carries the server's own backoff hint where it gave one, because Loki's
    ``Retry-After`` on a 429 is more accurate than any local guess.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PermanentError(GrafanaCloudError):
    """The operation will never succeed as submitted.

    A rejected label set, a malformed payload, a 401. Retrying wastes money and
    delays everything behind it.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ParseError(PermanentError):
    """An object or record could not be parsed into log lines."""
