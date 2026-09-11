"""Grafana Cloud Loki push client.

Standard library only, on purpose: an example that needs nothing but this module
produces a deployment package of a few tens of kilobytes, which uploads fast,
cold-starts fast, and has no third-party CVE surface to patch. Resist adding
`requests` here.

Uses the JSON push API rather than protobuf. The protobuf path is meaningfully
more efficient at high throughput, but it needs `snappy` and generated stubs,
and these examples are read as much as they are run. `docs/loki-ingestion.md`
says when to reach for protobuf instead.
"""

from __future__ import annotations

import gzip
import json
import random
import re
import time
import urllib.error
import urllib.request
from base64 import b64encode
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import LokiConfig
from .errors import PermanentError, RetryableError
from .log import get_logger

_LOG = get_logger(__name__)

# Loki's own label-name grammar. A label that fails this is rejected server-side
# with a 400, which retrying cannot fix.
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Hard ceiling on label count per stream. Loki's default server limit is higher,
# but a stream with this many labels is almost always a cardinality mistake.
MAX_LABELS_PER_STREAM = 15

# Label names that are a cardinality incident in every case we have seen. These
# values belong in the log line or in structured metadata, never in a label.
BANNED_LABEL_NAMES = frozenset(
    {
        "request_id",
        "requestid",
        "trace_id",
        "traceid",
        "span_id",
        "spanid",
        "user_id",
        "userid",
        "customer_id",
        "customerid",
        "session_id",
        "sessionid",
        "object_key",
        "objectkey",
        "s3_key",
        "key",
        "filename",
        "file_name",
        "path",
        "timestamp",
        "time",
        "date",
        "uuid",
        "id",
        "ip",
        "message",
        "line",
    }
)

_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class LogEntry:
    """One log line, at nanosecond precision.

    ``structured_metadata`` is per-line key/value data that Loki indexes cheaply
    and that does not create a stream. It is where anything high-cardinality
    goes: object key, request id, record offset.
    """

    timestamp_ns: int
    line: str
    structured_metadata: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def now(cls, line: str, **metadata: str) -> LogEntry:
        return cls(timestamp_ns=time.time_ns(), line=line, structured_metadata=metadata)

    def approximate_bytes(self) -> int:
        """Serialised size, near enough for batching decisions.

        Deliberately an estimate. Computing the exact JSON length per entry costs
        more than the occasional batch that lands 2% over the soft limit.
        """
        total = len(self.line.encode()) + 24
        for key, value in self.structured_metadata.items():
            total += len(key) + len(value) + 8
        return total


def validate_labels(labels: Mapping[str, str]) -> None:
    """Reject a label set that Loki will refuse, or that will cost a fortune.

    Raises PermanentError, because every failure here is a code or config bug
    that no amount of retrying changes.
    """
    if not labels:
        raise PermanentError("a Loki stream needs at least one label")
    if len(labels) > MAX_LABELS_PER_STREAM:
        raise PermanentError(
            f"{len(labels)} labels exceeds the {MAX_LABELS_PER_STREAM} label ceiling: "
            f"{sorted(labels)}. Move high-cardinality values to structured metadata."
        )
    for name, value in labels.items():
        if not _LABEL_NAME_RE.match(name):
            raise PermanentError(
                f"label name {name!r} is not valid Loki syntax (need [a-zA-Z_][a-zA-Z0-9_]*)"
            )
        if name.lower() in BANNED_LABEL_NAMES:
            raise PermanentError(
                f"label {name!r} is high-cardinality and banned. Pass it as structured "
                f"metadata on the entry instead. See docs/loki-ingestion.md."
            )
        if not value:
            raise PermanentError(f"label {name!r} has an empty value")


# One stream: its label set, and the entries belonging to it. A pair rather than
# a mapping keyed by labels, because a Mapping is not hashable and so can never be
# a dict key - an API that asked for one could not be called with a plain dict.
type Stream = tuple[Mapping[str, str], Sequence[LogEntry]]
type StreamBatch = Sequence[Stream]


class Opener(Protocol):
    """The one method this client needs from a urllib opener.

    A Protocol rather than urllib.request.OpenerDirector so a test can inject a
    fake without subclassing urllib internals or a `type: ignore`.
    """

    def open(self, request: Any, timeout: float | None = ...) -> Any: ...


def _stream_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


class LokiClient:
    """Pushes batches to one Loki tenant, with retry and backoff.

    The token is fetched through a callable rather than passed in, so the
    credential is resolved lazily on first push and can be re-resolved after a
    401 without rebuilding the client. That matters when a Cloud Access Policy
    token is rotated under a long-lived warm Lambda.
    """

    __slots__ = ("_auth_header", "_config", "_opener", "_token_provider")

    def __init__(
        self,
        config: LokiConfig,
        token_provider: Callable[[], str],
        *,
        opener: Opener | None = None,
    ) -> None:
        self._config = config
        self._token_provider = token_provider
        self._auth_header: str | None = None
        # Injectable so tests exercise the real request-building and retry paths
        # without a network or a mocking library.
        self._opener = opener or urllib.request.build_opener()

    def _authorization(self, *, refresh: bool = False) -> str:
        if self._auth_header is None or refresh:
            raw = f"{self._config.tenant_id}:{self._token_provider()}".encode()
            self._auth_header = f"Basic {b64encode(raw).decode()}"
        return self._auth_header

    def build_payload(self, streams: StreamBatch) -> bytes:
        """Serialise streams into a Loki push body.

        Entries are sorted ascending per stream. Loki accepts some out-of-order
        ingestion, but an unsorted batch can be partially rejected with a 400 that
        names only the first offending line, which is a miserable thing to debug.
        """
        merged: dict[tuple[tuple[str, str], ...], list[LogEntry]] = {}
        label_sets: dict[tuple[tuple[str, str], ...], dict[str, str]] = {}

        for labels, entries in streams:
            resolved = {**self._config.static_labels, **labels}
            validate_labels(resolved)
            key = _stream_key(resolved)
            label_sets[key] = resolved
            merged.setdefault(key, []).extend(entries)

        body: dict[str, Any] = {
            "streams": [
                {
                    "stream": label_sets[key],
                    "values": [
                        [str(entry.timestamp_ns), entry.line, dict(entry.structured_metadata)]
                        if entry.structured_metadata
                        else [str(entry.timestamp_ns), entry.line]
                        for entry in sorted(entries, key=lambda e: e.timestamp_ns)
                    ],
                }
                for key, entries in merged.items()
                if entries
            ]
        }
        return json.dumps(body, separators=(",", ":")).encode()

    def push(self, streams: StreamBatch) -> int:
        """Push one batch. Returns the number of lines accepted.

        Raises PermanentError for anything a retry cannot fix, and RetryableError
        only after the retry budget is spent - so the caller's own error handling
        sees a spent budget, not a transient blip.
        """
        streams = list(streams)
        payload = self.build_payload(streams)
        line_count = sum(len(entries) for _, entries in streams)
        if line_count == 0:
            return 0

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "grafana-cloud-reference-examples/loki-python",
        }
        sent = payload
        if self._config.compress:
            sent = gzip.compress(payload, compresslevel=6)
            headers["Content-Encoding"] = "gzip"

        last_error: Exception | None = None
        for attempt in range(1, self._config.max_retries + 1):
            headers["Authorization"] = self._authorization(refresh=attempt > 1)
            request = urllib.request.Request(  # noqa: S310 - scheme is validated https in config
                self._config.push_url, data=sent, headers=headers, method="POST"
            )
            try:
                with self._opener.open(
                    request, timeout=self._config.request_timeout_seconds
                ) as response:
                    status = int(response.status)
                if status in (200, 202, 204):
                    _LOG.debug(
                        "loki push accepted",
                        status=status,
                        lines=line_count,
                        streams=len(streams),
                        payload_bytes=len(sent),
                        attempt=attempt,
                    )
                    return line_count
                last_error = RetryableError(f"unexpected Loki status {status}")
            except urllib.error.HTTPError as exc:
                last_error = self._classify_http_error(exc, attempt=attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = RetryableError(f"Loki transport error: {exc}")
                _LOG.warning("loki push transport failure", attempt=attempt, cause=str(exc))

            if isinstance(last_error, PermanentError):
                raise last_error
            if attempt < self._config.max_retries:
                time.sleep(self._backoff_seconds(attempt, last_error))

        raise RetryableError(
            f"Loki push failed after {self._config.max_retries} attempts: {last_error}"
        )

    def _classify_http_error(self, exc: urllib.error.HTTPError, *, attempt: int) -> Exception:
        status = exc.code
        try:
            detail = exc.read().decode(errors="replace")[:512]
        except (OSError, ValueError):
            # ValueError: the body was already consumed and closed.
            detail = "<body unavailable>"
        finally:
            # An HTTPError owns a file object. Leaving it open leaks a descriptor
            # per failed push in a warm container, and raises a ResourceWarning
            # at some unrelated point later.
            exc.close()

        if status in _RETRYABLE_STATUSES:
            retry_after = _parse_retry_after(exc.headers.get("Retry-After"))
            _LOG.warning(
                "loki push rejected, will retry",
                status=status,
                attempt=attempt,
                retry_after_seconds=retry_after,
                detail=detail,
            )
            return RetryableError(f"Loki {status}: {detail}", retry_after_seconds=retry_after)

        if status in (401, 403):
            # Retry once with a freshly fetched token, in case of rotation, then
            # give up: a wrong scope on the access policy never recovers.
            if attempt == 1:
                _LOG.warning("loki push unauthorised, refreshing credential", status=status)
                self._auth_header = None
                return RetryableError(f"Loki {status}: {detail}")
            return PermanentError(
                f"Loki {status}: {detail}. Check the tenant id and that the Cloud "
                f"Access Policy token carries the logs:write scope.",
                status=status,
            )

        return PermanentError(f"Loki {status}: {detail}", status=status)

    def _backoff_seconds(self, attempt: int, error: Exception | None) -> float:
        """Exponential backoff with full jitter, capped, honouring Retry-After.

        Full jitter rather than a fixed multiplier because a fan-out of Lambdas
        triggered by one S3 prefix listing retries in lockstep otherwise, and
        hits the same rate limit again at the same instant.
        """
        if isinstance(error, RetryableError) and error.retry_after_seconds is not None:
            return min(error.retry_after_seconds, 30.0)
        ceiling = min(2.0 ** (attempt - 1), 30.0)
        return random.uniform(0.1, ceiling)  # noqa: S311 - jitter, not cryptography


def _parse_retry_after(value: str | None) -> float | None:
    """Parse the delta-seconds form of Retry-After. The HTTP-date form is ignored.

    Loki sends delta-seconds. Parsing the date form would need a clock-skew
    assumption that is worse than falling back to local backoff.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def group_by_labels(entries: Iterable[tuple[Mapping[str, str], LogEntry]]) -> list[Stream]:
    """Collapse (labels, entry) pairs into the stream batch push() wants."""
    grouped: dict[tuple[tuple[str, str], ...], list[LogEntry]] = {}
    originals: dict[tuple[tuple[str, str], ...], Mapping[str, str]] = {}
    for labels, entry in entries:
        key = _stream_key(labels)
        originals.setdefault(key, labels)
        grouped.setdefault(key, []).append(entry)
    return [(originals[key], value) for key, value in grouped.items()]
