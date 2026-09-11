"""Ship files landing in an S3 bucket to Grafana Cloud Loki.

Deliberately format-agnostic: plain text, JSON Lines, a JSON array or CSV, each
optionally gzipped. Everything specific to one vendor's export format belongs in
its own example (see ``examples/adobe-aem``), not here.

Everything expensive is built at module scope, so it happens once per execution
environment rather than once per invocation, and a bad configuration fails the
first invocation with a clear message instead of degrading quietly.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from grafana_cloud_common import (
    ConfigError,
    LogEntry,
    LokiClient,
    LokiConfig,
    StreamBatcher,
    configure,
    get_logger,
)
from grafana_cloud_common.aws import (
    CredentialProvider,
    LambdaContext,
    RecordFormat,
    S3ObjectReader,
    S3ObjectRef,
    parse_event,
    process_messages,
)

configure()
_LOG = get_logger(__name__)

CONFIG = LokiConfig.from_env()
CREDENTIALS = CredentialProvider(CONFIG.credentials_secret_id, static_token=CONFIG.token)
CLIENT = LokiClient(CONFIG, CREDENTIALS.token)
READER = S3ObjectReader()

# Format override. "auto" infers from the object key, which is right when the
# producer names files sensibly and wrong when it writes JSON Lines to a ".txt".
RECORD_FORMAT_OVERRIDE = os.environ.get("RECORD_FORMAT", "auto").strip().lower()

# Optional timestamp extraction, for JSONL and CSV records that carry their own
# event time. Left unset, every line is stamped with ingestion time.
TIMESTAMP_FIELD = (os.environ.get("TIMESTAMP_FIELD") or "").strip() or None
TIMESTAMP_FORMAT = (os.environ.get("TIMESTAMP_FORMAT") or "rfc3339").strip().lower()

# Enforced here rather than at the event source, because an EventBridge rule
# cannot AND a suffix matcher onto a prefix matcher - multiple matchers on one
# field are OR'd, which widens the filter instead of narrowing it. The S3
# notification path does support both as real filter rules, so this is a no-op
# there. Empty means accept any key.
SOURCE_KEY_SUFFIX = (os.environ.get("SOURCE_KEY_SUFFIX") or "").strip()

# How many leading key segments become a `prefix` label. 0 disables it. Use 1 or
# 2 for a bucket laid out as `<team>/<source>/<date>/...`; never enough depth to
# reach a date or an object name, which would be one stream per file.
PREFIX_LABEL_DEPTH = int(os.environ.get("PREFIX_LABEL_DEPTH", "0"))

# Loki rejects samples older than the tenant's reject_old_samples_max_age (one
# week on Grafana Cloud by default) with a 400. When extracting timestamps from
# a backfill, either raise that limit on the tenant or leave extraction off.
MAX_TIMESTAMP_AGE_SECONDS = float(os.environ.get("MAX_TIMESTAMP_AGE_SECONDS", "0")) or None

if PREFIX_LABEL_DEPTH < 0:
    raise ConfigError("PREFIX_LABEL_DEPTH must be zero or positive")
if RECORD_FORMAT_OVERRIDE != "auto" and RECORD_FORMAT_OVERRIDE not in set(RecordFormat):
    raise ConfigError(
        f"RECORD_FORMAT must be 'auto' or one of {sorted(str(f) for f in RecordFormat)}, "
        f"got {RECORD_FORMAT_OVERRIDE!r}"
    )


def _resolved_format() -> RecordFormat | None:
    """None means 'let the reader infer it from the key'."""
    if RECORD_FORMAT_OVERRIDE == "auto":
        return None
    return RecordFormat(RECORD_FORMAT_OVERRIDE)


def labels_for(ref: S3ObjectRef) -> dict[str, str]:
    """Build the stream labels for one object.

    Only low-cardinality dimensions. The object key, the record number and the
    version id go into structured metadata on each entry instead, because a label
    derived from a key is one Loki stream per file.
    """
    labels = {"service_name": os.environ.get("SERVICE_NAME", "generic-s3"), "bucket": ref.bucket}
    if PREFIX_LABEL_DEPTH:
        segments = ref.key.split("/")[:PREFIX_LABEL_DEPTH]
        prefix = "/".join(segments)
        if prefix and len(segments) == PREFIX_LABEL_DEPTH:
            labels["prefix"] = prefix
    return labels


def _parse_timestamp(raw: object) -> int | None:
    """Parse an extracted timestamp to epoch nanoseconds, or None if unusable.

    Returning None rather than raising: one record with a malformed timestamp
    should be stamped with ingestion time and shipped, not sink the whole object.
    """
    if raw is None or raw == "":
        return None
    text = str(raw)
    try:
        match TIMESTAMP_FORMAT:
            case "epoch_s":
                return int(float(text) * 1_000_000_000)
            case "epoch_ms":
                return int(float(text) * 1_000_000)
            case "epoch_us":
                return int(float(text) * 1_000)
            case "epoch_ns":
                # Not via float(): a nanosecond epoch is 19 digits and float64
                # carries about 16, so going through a float silently rounds the
                # timestamp to the nearest few hundred nanoseconds.
                return int(text)
            case "rfc3339":
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timestamp has no timezone; Loki needs an absolute instant")
                return int(parsed.timestamp() * 1_000_000_000)
            case _:
                parsed = datetime.strptime(text, TIMESTAMP_FORMAT)  # noqa: DTZ007
                if parsed.tzinfo is None:
                    raise ValueError("timestamp has no timezone; Loki needs an absolute instant")
                return int(parsed.timestamp() * 1_000_000_000)
    except (TypeError, ValueError, OverflowError) as exc:
        # OverflowError, not ValueError, is what int(float("inf")) raises - and
        # "Infinity" is valid JSON to Python's decoder.
        _LOG.warning("unparseable timestamp, using ingestion time", cause=str(exc))
        return None


def _timestamp_for(line: str, now_ns: int) -> int:
    if TIMESTAMP_FIELD is None:
        return now_ns
    try:
        document: Any = json.loads(line)
    except json.JSONDecodeError:
        return now_ns
    if not isinstance(document, dict):
        return now_ns

    extracted = _parse_timestamp(document.get(TIMESTAMP_FIELD))
    if extracted is None:
        return now_ns
    if MAX_TIMESTAMP_AGE_SECONDS is not None:
        age_seconds = (now_ns - extracted) / 1_000_000_000
        if age_seconds > MAX_TIMESTAMP_AGE_SECONDS:
            _LOG.warning(
                "extracted timestamp older than the configured limit, using ingestion time",
                age_seconds=round(age_seconds, 1),
                limit_seconds=MAX_TIMESTAMP_AGE_SECONDS,
            )
            return now_ns
    return extracted


def _entries(ref: S3ObjectRef) -> Iterator[tuple[dict[str, str], LogEntry]]:
    labels = labels_for(ref)
    now_ns = time.time_ns()
    for record_number, line in READER.lines(ref, record_format=_resolved_format()):
        metadata = {"object_key": ref.key, "record": str(record_number)}
        if ref.version_id:
            metadata["object_version_id"] = ref.version_id
        yield (
            labels,
            LogEntry(
                timestamp_ns=_timestamp_for(line, now_ns),
                line=line,
                structured_metadata=metadata,
            ),
        )


def ship_object(ref: S3ObjectRef) -> int:
    """Read one object and push every line. Returns the line count."""
    if SOURCE_KEY_SUFFIX and not ref.key.endswith(SOURCE_KEY_SUFFIX):
        _LOG.debug("skipping object: key does not match SOURCE_KEY_SUFFIX", uri=ref.uri)
        return 0
    if ref.size_bytes == 0:
        _LOG.info("skipping zero-byte object", uri=ref.uri)
        return 0

    batcher = StreamBatcher(max_lines=CONFIG.batch_max_lines, max_bytes=CONFIG.batch_max_bytes)
    shipped = 0
    for batch in batcher.drain(_entries(ref)):
        shipped += CLIENT.push(batch)
    _LOG.info("object shipped", uri=ref.uri, lines=shipped)
    return shipped


def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Entry point. Configure the function's handler as ``generic_s3.handler.lambda_handler``.

    Always returns the SQS partial-batch-failure response. It is ignored for a
    direct S3 invocation, and for an SQS source it is the difference between
    redelivering one bad message and redelivering the whole batch - which would
    duplicate every line already shipped.
    """
    log = _LOG.bind(aws_request_id=getattr(context, "aws_request_id", "local"))
    messages = parse_event(event)
    if not messages:
        log.info("event carried no objects; nothing to do")
        return {"batchItemFailures": []}

    outcome = process_messages(messages, ship_object, context=context)
    log.info(
        "invocation complete",
        objects_processed=outcome.objects_processed,
        objects_failed=outcome.objects_failed,
        lines_shipped=outcome.lines_shipped,
        messages_failed=len(outcome.failed_message_ids),
    )
    return outcome.to_response()
