"""Ship Adobe Experience Manager Cloud Service logs from S3 to Grafana Cloud Loki.

AEM Cloud Service forwards seven log types, in six different formats, from three
service tiers. This parses each one, labels the stream by deployment coordinates
only, and puts every extracted field into structured metadata so a dashboard can
aggregate on it without a parse stage.

Read `../../README.md` for the label contract and the cost reasoning. The short
version: labels are `service_name`, `aem_program_id`, `aem_env_id`,
`aem_env_type`, `aem_tier`, `log_type` and - where AEM assigned one - `level`.
Nothing derived from a path, an IP, a request id or a timestamp is ever a label.

Everything expensive is built at module scope so it happens once per execution
environment, and a bad configuration fails the first invocation loudly rather
than degrading quietly.
"""

from __future__ import annotations

import itertools
import os
import time
from collections.abc import Iterator
from dataclasses import replace
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

from .classify import (
    DEFAULT_KEY_PATTERN,
    ClassificationError,
    compile_key_pattern,
    from_key,
    log_type_from_name,
    sniff_log_type,
    tier_from_name,
)
from .correlate import RequestCorrelator
from .labels import LABEL_LEVEL, build_labels, detected_level_for
from .logtypes import LogType, Tier
from .parsers import ParsedRecord, parser_for

configure()
_LOG = get_logger(__name__)

CONFIG = LokiConfig.from_env()
CREDENTIALS = CredentialProvider(CONFIG.credentials_secret_id, static_token=CONFIG.token)
CLIENT = LokiClient(CONFIG, CREDENTIALS.token)
READER = S3ObjectReader()

SERVICE_NAME = os.environ.get("SERVICE_NAME", "adobe-aem")

# How tier, log type and the environment coordinates are read off an object key.
# See classify.py for why this is configurable rather than fixed: Adobe does not
# document one S3 layout, and a wrong guess silently sends everything to the
# `unknown` stream.
KEY_PATTERN = os.environ.get("KEY_PATTERN") or DEFAULT_KEY_PATTERN

# Fallbacks for the coordinates the key does not carry. The program and
# environment ids are stable per deployment and usually not in the key at all,
# so in practice these are how they get set. Empty means the label is omitted
# rather than set to a placeholder - a label whose value is "unknown" still
# creates a stream and still looks like data on a dashboard.
DEFAULT_PROGRAM_ID = (os.environ.get("AEM_PROGRAM_ID") or "").strip()
DEFAULT_ENV_ID = (os.environ.get("AEM_ENV_ID") or "").strip()
DEFAULT_ENV_TYPE = (os.environ.get("AEM_ENV_TYPE") or "").strip()
DEFAULT_TIER = (os.environ.get("AEM_TIER") or "").strip()

# `raw` ships the original line; `message` ships only the human-readable part.
# See the parsers module docstring for the trade-off.
LINE_CONTENT = (os.environ.get("LINE_CONTENT") or "raw").strip().lower()

# Read the content of the first line to identify a log type the key pattern did
# not. Costs nothing extra - the line is read either way - and is the difference
# between a working deployment and an `unknown` stream when the key layout is not
# what was configured. Turn it off only to force strict key-pattern behaviour.
SNIFF_CONTENT = (os.environ.get("SNIFF_CONTENT") or "true").strip().lower() != "false"

# AEM's error log and Apache's error log carry no timezone. AEM Cloud Service
# writes UTC; a self-hosted AEM might not.
LOG_UTC_OFFSET_SECONDS = int(os.environ.get("LOG_UTC_OFFSET_SECONDS", "0"))

# Loki rejects a sample older than the tenant's `reject_old_samples_max_age`
# (one week on Grafana Cloud by default) with a 400 that no retry fixes. AEM log
# forwarding is near-real-time so this does not bite in normal operation, but
# replaying a historical export absolutely does - and the failure looks like a
# broken function rather than a rejected timestamp. Set this and old lines are
# stamped at ingestion instead of being lost.
MAX_TIMESTAMP_AGE_SECONDS = float(os.environ.get("MAX_TIMESTAMP_AGE_SECONDS", "0")) or None

# Correlate the AEM request log's `->` and `<-` line pairs. On by default: it is
# what makes latency-by-path possible at all.
CORRELATE_REQUESTS = (os.environ.get("CORRELATE_REQUESTS") or "true").strip().lower() != "false"
MAX_PENDING_REQUESTS = int(os.environ.get("MAX_PENDING_REQUESTS", "20000"))

# Client IP is in every access and CDN line and is personal data under GDPR.
# Keeping it is the default because it is load-bearing for abuse and geography
# panels, and because it is the customer's own traffic - but a deployment that
# must not store it should not have to fork this example to drop it.
DROP_CLIENT_IP = (os.environ.get("DROP_CLIENT_IP") or "false").strip().lower() == "true"

# Enforced here rather than at the event source, because an EventBridge rule
# cannot AND a suffix matcher onto a prefix matcher - multiple matchers on one
# field are OR'd, which widens the filter instead of narrowing it.
SOURCE_KEY_SUFFIX = (os.environ.get("SOURCE_KEY_SUFFIX") or "").strip()

if LINE_CONTENT not in ("raw", "message"):
    raise ConfigError(f"LINE_CONTENT must be 'raw' or 'message', got {LINE_CONTENT!r}")
if MAX_PENDING_REQUESTS < 1:
    raise ConfigError("MAX_PENDING_REQUESTS must be at least 1")

try:
    COMPILED_KEY_PATTERN = compile_key_pattern(KEY_PATTERN)
except ClassificationError as exc:
    raise ConfigError(str(exc)) from exc


def labels_for(
    *, log_type: LogType, tier: Tier, coordinates: dict[str, str], level: str | None
) -> dict[str, str]:
    """Bind this deployment's configuration to the shared label contract.

    The rules live in `labels.build_labels`, which imports no configuration, so
    the tests and the local dev runner can build identical labels without
    constructing a Loki client. See that module for why each label is or is not
    in the set.
    """
    return build_labels(
        service_name=SERVICE_NAME,
        log_type=log_type,
        tier=tier,
        coordinates=coordinates,
        level=level,
        default_program_id=DEFAULT_PROGRAM_ID,
        default_env_id=DEFAULT_ENV_ID,
        default_env_type=DEFAULT_ENV_TYPE,
    )


def _resolve(ref: S3ObjectRef, first_line: str | None) -> tuple[LogType, Tier, dict[str, str]]:
    """Decide the log type, tier and coordinates for an object.

    The key wins where it says anything, because it describes the whole object.
    Content sniffing only fills a log type the key did not give.
    """
    coordinates = from_key(ref.key, COMPILED_KEY_PATTERN)
    log_type = (
        log_type_from_name(coordinates["log_type"])
        if "log_type" in coordinates
        else LogType.UNKNOWN
    )
    tier = tier_from_name(coordinates["tier"]) if "tier" in coordinates else Tier.UNKNOWN

    if log_type is LogType.UNKNOWN and SNIFF_CONTENT and first_line is not None:
        log_type = sniff_log_type(first_line)
        if log_type is not LogType.UNKNOWN:
            _LOG.info(
                "log type identified by content, not key",
                uri=ref.uri,
                log_type=str(log_type),
                hint="set KEY_PATTERN to match your bucket layout to avoid sniffing",
            )

    if tier is Tier.UNKNOWN and DEFAULT_TIER:
        tier = tier_from_name(DEFAULT_TIER)
    # A dispatcher log is written by the dispatcher, whatever the key says.
    if log_type is LogType.AEM_DISPATCHER and tier is Tier.UNKNOWN:
        tier = Tier.DISPATCHER
    return log_type, tier, coordinates


def _timestamp_for(record: ParsedRecord, now_ns: int) -> int:
    """Pick the timestamp to ship, falling back to ingestion time.

    Falls back when the line carried none, and when the extracted one is older
    than `MAX_TIMESTAMP_AGE_SECONDS`. The second case is the one that matters:
    Loki 400s a too-old sample and drops the whole push, so one stale line would
    otherwise take a batch of good ones with it.
    """
    if record.timestamp_ns is None:
        return now_ns
    if MAX_TIMESTAMP_AGE_SECONDS is not None:
        age_seconds = (now_ns - record.timestamp_ns) / 1_000_000_000
        if age_seconds > MAX_TIMESTAMP_AGE_SECONDS:
            return now_ns
    return record.timestamp_ns


def _entries(ref: S3ObjectRef) -> Iterator[tuple[dict[str, str], LogEntry]]:
    """Parse one object into labelled Loki entries.

    A generator so a large object is never fully in memory: the reader streams,
    this parses one line at a time, and the batcher flushes on its own limits.
    """
    # Read as lines regardless of the key's extension. An AEM log is
    # line-oriented even when it is JSON Lines, and the CDN log arrives gzipped
    # with a `.log.gz` name that the shared reader's own detection handles.
    line_stream = READER.lines(ref, record_format=RecordFormat.LINES)

    first: tuple[int, str] | None = None
    for item in line_stream:
        first = item
        break
    if first is None:
        _LOG.info("object held no lines", uri=ref.uri)
        return

    log_type, tier, coordinates = _resolve(ref, first[1])
    parse = parser_for(log_type, utc_offset_seconds=LOG_UTC_OFFSET_SECONDS)
    correlator = (
        RequestCorrelator(max_pending=MAX_PENDING_REQUESTS)
        if CORRELATE_REQUESTS and log_type is LogType.AEM_REQUEST
        else None
    )

    # `object_key` goes in structured metadata rather than a label: one stream
    # per file is the single most expensive mistake available here, and a daily
    # log per tier per type is already hundreds of files a month.
    base_metadata = {"object_key": ref.key, "bucket": ref.bucket}

    now_ns = time.time_ns()
    unmatched = 0
    count = 0

    def build(record: ParsedRecord) -> tuple[dict[str, str], LogEntry]:
        metadata = {**base_metadata, **record.metadata}
        if DROP_CLIENT_IP:
            metadata.pop("client_ip", None)
        labels = labels_for(
            log_type=log_type, tier=tier, coordinates=coordinates, level=record.level
        )
        # A log type with no severity of its own still gets a `detected_level`,
        # inferred from the status class. Metadata, never the label - see
        # labels.STATUS_CLASS_LEVELS for why, and for the measurement that
        # showed Grafana Cloud otherwise fills this in as "unknown".
        if LABEL_LEVEL not in labels:
            inferred = record.level or detected_level_for(metadata)
            if inferred:
                metadata["detected_level"] = inferred
        return labels, LogEntry(
            timestamp_ns=_timestamp_for(record, now_ns),
            line=record.message,
            structured_metadata=metadata,
        )

    # `chain`, not `(first, *line_stream)`: unpacking a generator into a tuple
    # reads the entire object into memory, which is exactly what streaming the
    # first line off separately was meant to avoid.
    for record_number, line in itertools.chain((first,), line_stream):
        parsed = parse(line)
        # `record` is attached here rather than at build time, for the same
        # reason the line content is resolved here: the correlator buffers a
        # record and releases it during a later iteration, so anything read from
        # the loop variable at build time would be the wrong line's. A flushed
        # entry was getting record="0".
        parsed = replace(parsed, metadata={**parsed.metadata, "record": str(record_number)})
        if LINE_CONTENT == "raw":
            # Resolved here, once, rather than at the point the entry is built.
            # The correlator buffers records and releases them later, so a
            # record can outlive the loop iteration that read its line - and
            # reaching back for `line` at build time shipped the parsed
            # fragment instead of the original for every buffered response.
            parsed = replace(parsed, message=line)
        # The correlator turns one input line into zero, one or two records: a
        # response with no request yet is held back, and the request that
        # releases it emits both. Everything else is a one-element list.
        records = correlator.process(parsed) if correlator is not None else [parsed]
        for record in records:
            labels, entry = build(record)
            if entry.structured_metadata.get("parse_status") == "unmatched":
                unmatched += 1
            count += 1
            yield labels, entry

    if correlator is not None:
        # Whatever is still held when the object ends is genuinely unpairable -
        # its partner line is in another file. Emitted, never dropped.
        for record in correlator.flush():
            labels, entry = build(record)
            count += 1
            yield labels, entry

    if unmatched:
        # Loud, because it means a format this example does not handle is being
        # shipped as opaque text. The ratio is what matters, so both numbers are
        # logged.
        _LOG.warning(
            "lines did not match the expected format",
            uri=ref.uri,
            log_type=str(log_type),
            unmatched=unmatched,
            total=count,
        )
    if correlator is not None:
        _LOG.info(
            "request correlation finished",
            uri=ref.uri,
            paired=correlator.stats.paired,
            unpaired_responses=correlator.stats.unpaired_responses,
            evicted=correlator.stats.evicted,
            pending_at_end=correlator.pending,
        )


def handle_object(ref: S3ObjectRef) -> int:
    """Ship one S3 object. Returns the number of lines pushed."""
    if SOURCE_KEY_SUFFIX and not ref.key.endswith(SOURCE_KEY_SUFFIX):
        _LOG.info("skipping object", uri=ref.uri, reason="suffix", suffix=SOURCE_KEY_SUFFIX)
        return 0

    batcher = StreamBatcher(max_lines=CONFIG.batch_max_lines, max_bytes=CONFIG.batch_max_bytes)
    shipped = 0
    for batch in batcher.drain(_entries(ref)):
        shipped += CLIENT.push(batch)
    _LOG.info("object shipped", uri=ref.uri, lines=shipped)
    return shipped


def lambda_handler(event: dict[str, Any], context: LambdaContext | None = None) -> dict[str, Any]:
    """Lambda entry point.

    Returns an SQS partial-batch-failure response, which only takes effect when
    the event source mapping sets `FunctionResponseTypes =
    ["ReportBatchItemFailures"]`. Both IaC paths set it; without it Lambda
    redelivers the whole batch and duplicates every line already shipped.
    """
    outcome = process_messages(parse_event(event), handle_object, context=context)
    _LOG.info(
        "invocation complete",
        objects_processed=outcome.objects_processed,
        objects_failed=outcome.objects_failed,
        lines_shipped=outcome.lines_shipped,
        failed_messages=len(outcome.failed_message_ids),
    )
    return outcome.to_response()
