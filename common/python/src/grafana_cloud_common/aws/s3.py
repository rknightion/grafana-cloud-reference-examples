"""Stream an S3 object into log lines without buffering the whole thing.

The object is read as a stream and decompressed on the fly, so a 2 GB gzipped
export runs in a 256 MB function. Nothing here calls ``.read()`` without a bound.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Any, Protocol, cast

import boto3
from botocore.exceptions import ClientError

from grafana_cloud_common.errors import ParseError, PermanentError, RetryableError
from grafana_cloud_common.log import get_logger

_LOG = get_logger(__name__)


class S3Sender(Protocol):
    """The two calls this module makes.

    A Protocol rather than the boto3 stub type, so a test double is a handful of
    lines and no call site needs a `type: ignore`. Errors are caught as
    botocore's ClientError rather than via `client.exceptions.<Name>`, because S3
    does not model every failure it can return as a named class.
    """

    def get_object(self, **kwargs: Any) -> Any: ...

    def head_object(self, **kwargs: Any) -> Any: ...


# Read in chunks rather than line-by-line off the socket. 1 MB is a good trade
# between syscall count and peak memory for text log data.
_READ_CHUNK_BYTES = 1024 * 1024

_GZIP_SUFFIXES = (".gz", ".gzip")


class RecordFormat(StrEnum):
    """How to turn the object's bytes into one log line per record."""

    LINES = "lines"
    """One log line per input line. The default, and correct for most log files."""

    JSONL = "jsonl"
    """One JSON document per line. Validated, then re-emitted compactly."""

    JSON_ARRAY = "json_array"
    """A single JSON array; each element becomes one line. Read whole, so bounded
    by max_json_bytes - a JSON array cannot be streamed without a pull parser."""

    CSV = "csv"
    """Header row plus data rows; each row becomes a JSON object keyed by header."""


@dataclass(frozen=True, slots=True)
class S3ObjectRef:
    """A specific version of a specific object."""

    bucket: str
    key: str
    size_bytes: int | None = None
    version_id: str | None = None

    @property
    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


def detect_format(key: str) -> RecordFormat:
    """Guess the record format from the key, ignoring any compression suffix.

    A guess, not a contract. An example whose input format is known should set it
    explicitly rather than relying on this.
    """
    lowered = key.lower()
    for suffix in _GZIP_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break

    if lowered.endswith((".jsonl", ".ndjson")):
        return RecordFormat.JSONL
    if lowered.endswith(".json"):
        return RecordFormat.JSON_ARRAY
    if lowered.endswith((".csv", ".tsv")):
        return RecordFormat.CSV
    return RecordFormat.LINES


def is_compressed(key: str) -> bool:
    return key.lower().endswith(_GZIP_SUFFIXES)


class S3ObjectReader:
    """Reads S3 objects as an iterator of log lines.

    One instance per execution environment: the boto3 client holds a connection
    pool, and rebuilding it per invocation adds a TLS handshake to every call.
    """

    __slots__ = ("_client", "_encoding", "_max_json_bytes")

    def __init__(
        self,
        *,
        client: S3Sender | None = None,
        encoding: str = "utf-8",
        max_json_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self._client = client
        self._encoding = encoding
        self._max_json_bytes = max_json_bytes

    def _s3(self) -> S3Sender:
        # Assigned through a local so the None is narrowed away for the type
        # checker; the attribute itself stays optional for lazy construction.
        client = self._client
        if client is None:
            # cast, because boto3-stubs types get_object with explicit keyword
            # parameters while the Protocol promises **kwargs, and a fixed
            # signature does not structurally satisfy a **kwargs one. The
            # Protocol is the useful shape for callers and for test doubles.
            client = cast(S3Sender, boto3.client("s3"))
            self._client = client
        return client

    def head(self, ref: S3ObjectRef) -> dict[str, Any]:
        """HeadObject, with the errors classified. Useful to skip a zero-byte
        object before paying for a GET."""
        client = self._s3()
        kwargs: dict[str, Any] = {"Bucket": ref.bucket, "Key": ref.key}
        if ref.version_id:
            kwargs["VersionId"] = ref.version_id
        try:
            return dict(client.head_object(**kwargs))
        except ClientError as exc:
            raise _classify_s3_error(exc, ref) from exc

    def lines(
        self, ref: S3ObjectRef, *, record_format: RecordFormat | None = None
    ) -> Iterator[tuple[int, str]]:
        """Yield ``(record_number, line)`` pairs, 1-indexed.

        The record number is the caller's only handle on where a line came from,
        so it goes into structured metadata rather than being discarded. Blank
        lines are skipped; Loki has no use for them and they still cost ingest.
        """
        resolved = record_format or detect_format(ref.key)
        client = self._s3()
        kwargs: dict[str, Any] = {"Bucket": ref.bucket, "Key": ref.key}
        if ref.version_id:
            kwargs["VersionId"] = ref.version_id

        try:
            response = client.get_object(**kwargs)
        except ClientError as exc:
            raise _classify_s3_error(exc, ref) from exc

        body = response["Body"]
        try:
            stream: Any = gzip.GzipFile(fileobj=body) if is_compressed(ref.key) else body
            buffered = io.BufferedReader(_Readable(stream), buffer_size=_READ_CHUNK_BYTES)
            _LOG.debug("reading s3 object", uri=ref.uri, record_format=str(resolved))

            if resolved is RecordFormat.JSON_ARRAY:
                # Bounded on bytes read off the stream, not on characters read
                # through the decoder: text.read(n) counts characters, so a
                # multi-byte document could be several times the intended bound
                # in memory.
                yield from self._json_array(buffered, ref)
                return

            text = io.TextIOWrapper(
                buffered,
                encoding=self._encoding,
                # A single bad byte must not lose the rest of a customer's file.
                errors="replace",
                newline="",
            )
            yield from self._records(text, resolved, ref)
        except gzip.BadGzipFile as exc:
            # The object is not gzip despite its key. No retry can change that.
            raise ParseError(f"{ref.uri}: not a gzip stream: {exc}") from exc
        except (OSError, EOFError) as exc:
            # Everything else that surfaces here is a read failure rather than a
            # content failure: a socket reset, a read timeout, or a stream that
            # ended early. Classified retryable, because treating a transport
            # blip as permanent means the object is never processed at all. A
            # genuinely truncated object still lands in the DLQ once the
            # redelivery count is spent.
            raise RetryableError(f"{ref.uri}: read failed: {exc}") from exc
        finally:
            body.close()

    def _records(
        self, text: io.TextIOWrapper, record_format: RecordFormat, ref: S3ObjectRef
    ) -> Iterator[tuple[int, str]]:
        match record_format:
            case RecordFormat.LINES:
                index = 0
                for raw in text:
                    line = raw.rstrip("\r\n")
                    if not line.strip():
                        continue
                    index += 1
                    yield index, line

            case RecordFormat.JSONL:
                index = 0
                for line_number, raw in enumerate(text, start=1):
                    line = raw.strip()
                    if not line:
                        continue
                    index += 1
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ParseError(
                            f"{ref.uri}: line {line_number} is not valid JSON: {exc}"
                        ) from exc
                    yield index, json.dumps(parsed, separators=(",", ":"), default=str)

            case RecordFormat.CSV:
                base = ref.key.lower()
                for suffix in _GZIP_SUFFIXES:
                    base = base.removesuffix(suffix)
                delimiter = "\t" if base.endswith(".tsv") else ","
                reader = csv.DictReader(text, delimiter=delimiter)
                if reader.fieldnames is None:
                    raise ParseError(f"{ref.uri}: CSV has no header row")
                for index, row in enumerate(reader, start=1):
                    # None keys appear when a row has more fields than the header;
                    # keep the data, make the anomaly visible in the line itself.
                    cleaned = {
                        (key if key is not None else "_extra"): value for key, value in row.items()
                    }
                    yield index, json.dumps(cleaned, separators=(",", ":"), default=str)

    def _json_array(self, stream: IO[bytes], ref: S3ObjectRef) -> Iterator[tuple[int, str]]:
        """Read a whole JSON document, bounded in bytes, and yield its elements.

        Streaming a JSON array needs a pull parser and therefore a dependency.
        The byte bound is the honest alternative: it fails with a message that
        says what to do instead of exhausting the function's memory.
        """
        payload = stream.read(self._max_json_bytes + 1)
        if len(payload) > self._max_json_bytes:
            raise ParseError(
                f"{ref.uri}: JSON document exceeds max_json_bytes "
                f"({self._max_json_bytes} bytes). Re-export as JSON Lines."
            )
        try:
            document = json.loads(payload.decode(self._encoding, errors="replace"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"{ref.uri}: not valid JSON: {exc}") from exc

        elements = document if isinstance(document, list) else [document]
        for index, element in enumerate(elements, start=1):
            yield index, json.dumps(element, separators=(",", ":"), default=str)


class _Readable(io.RawIOBase):
    """Adapts a botocore StreamingBody (or GzipFile) to a raw stream.

    botocore's StreamingBody is not an io.RawIOBase, so BufferedReader and
    TextIOWrapper will not wrap it directly. This is the smallest adapter that
    keeps the read streaming rather than pulling the object into memory.
    """

    __slots__ = ("_source",)

    def __init__(self, source: Any) -> None:
        super().__init__()
        self._source = source

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._source.read(len(buffer))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _classify_s3_error(exc: ClientError, ref: S3ObjectRef) -> Exception:
    """Map a botocore ClientError to the retryable/permanent split.

    NoSuchKey is permanent and common: an object deleted between the S3 event and
    the invocation. Retrying it burns the whole redrive budget for nothing.
    """
    code = str(exc.response.get("Error", {}).get("Code", ""))
    status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))

    if code in {"NoSuchKey", "NoSuchVersion", "404"} or status == 404:
        return PermanentError(f"{ref.uri}: object does not exist (deleted after the event?)")
    if code in {"AccessDenied", "403"} or status == 403:
        return PermanentError(
            f"{ref.uri}: access denied. The function role needs s3:GetObject on this "
            f"prefix, and kms:Decrypt if the bucket uses SSE-KMS."
        )
    if (
        code in {"SlowDown", "RequestTimeout", "ServiceUnavailable", "InternalError"}
        or status >= 500
    ):
        return RetryableError(f"{ref.uri}: S3 transient failure ({code or status})")
    return PermanentError(f"{ref.uri}: S3 error {code or status}")
