"""Test doubles for exercising the Loki client and the S3 reader.

Part of the library rather than a per-example conftest, so every example tests
against the same fakes and nobody reinvents them. `just package` excludes this
module from the release zip.

Deliberately hand-rolled rather than pulling in `moto` or `responses`: these
fakes are short, they make assertions readable, and they keep `uv sync` fast.

Standard library only, so it imports without pytest installed. Nothing in the
shipped code path imports it.
"""

from __future__ import annotations

import gzip
import io
import urllib.error
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import LokiConfig


@dataclass
class RecordedRequest:
    url: str
    body: bytes
    headers: dict[str, str]


@dataclass
class FakeOpener:
    """Stands in for a urllib OpenerDirector.

    ``responses`` is consumed in order. An int is a status code; an exception is
    raised. Anything left over at the end of a test means the code under test
    made fewer calls than expected, which the assertions should catch.
    """

    responses: list[int | Exception]
    recorded: list[RecordedRequest] = field(default_factory=list)

    def open(self, request: Any, timeout: float | None = None) -> Any:
        self.recorded.append(
            RecordedRequest(
                url=request.full_url,
                body=request.data,
                headers={key.lower(): value for key, value in request.headers.items()},
            )
        )
        if not self.responses:
            raise AssertionError("FakeOpener ran out of scripted responses")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _FakeResponse(nxt)


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def http_error(status: int, *, body: bytes = b"nope", retry_after: str | None = None) -> Exception:
    """Build a real HTTPError, so the code under test exercises the real type."""
    headers: dict[str, str] = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://logs.example.net/loki/api/v1/push",
        code=status,
        msg="scripted",
        hdrs=headers,  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


def loki_config(**overrides: object) -> LokiConfig:
    """A valid config for tests. Override any field by keyword."""
    defaults: dict[str, object] = {
        "push_url": "https://logs-prod-012.grafana.net/loki/api/v1/push",
        "tenant_id": "123456",
        "credentials_secret_id": None,
        "token": "glc_fake",
        "static_labels": {},
        "batch_max_lines": 3,
        "batch_max_bytes": 1_000_000,
        "request_timeout_seconds": 1.0,
        "max_retries": 3,
        "compress": False,
        "log_level": "DEBUG",
    }
    defaults.update(overrides)
    return LokiConfig(**defaults)  # type: ignore[arg-type]


class FakeS3Exceptions:
    ClientError = type("ClientError", (Exception,), {"response": {}})


class FakeS3Client:
    """Minimal S3 client: one object, served as a stream."""

    def __init__(self, payload: bytes, *, gzipped: bool = False) -> None:
        self.payload = gzip.compress(payload) if gzipped else payload
        self.exceptions = FakeS3Exceptions()
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Body": _ClosableStream(self.payload)}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"ContentLength": len(self.payload)}


class _ClosableStream(io.BytesIO):
    closed_by_reader = False

    def close(self) -> None:
        type(self).closed_by_reader = True
        super().close()


def lines_of(pairs: Iterator[tuple[int, str]]) -> Sequence[str]:
    return [line for _, line in pairs]
