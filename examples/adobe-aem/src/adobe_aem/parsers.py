"""One parser per AEM Cloud Service log type.

Each parser turns a raw line into a `ParsedRecord`: an event timestamp, the
message, an optional level, and the fields worth querying on. **No parser ever
drops or rejects a line.** An unrecognised line comes back with the raw text as
its message and `parse_status="unmatched"` in its metadata, so it still reaches
Loki and still shows up in a dashboard panel that counts unmatched lines. A log
shipper that silently discards what it cannot parse is worse than one that does
no parsing at all, because the gap is invisible.

## Where each field goes, and why

The split between a Loki *label* and *structured metadata* is the whole point of
this example, so it is worth stating plainly.

A label partitions streams, and stream count is the product of every label's
cardinality. `status` looks like an obvious label - it is a small set - but
`status` x `method` x `path` is not, and each of those alone multiplies every
other label already present. So the labels here are only the deployment
coordinates: which environment, which tier, which log type, and a severity where
AEM assigned one. Everything a dashboard actually aggregates by goes into
structured metadata, which Loki indexes per-entry without creating a stream and
which `sum by (...)` and `unwrap` both read directly - no `| json` or
`| regexp` parse stage, so a panel stays cheap at any volume.

That is what makes this better than a raw shipper: the fields are already
extracted at write time, once, instead of being re-parsed on every query by
every viewer.

## The message is not the whole line by default

`LINE_CONTENT=raw` ships the original line verbatim, which duplicates the
timestamp and node id already present in metadata - about 40% waste on an error
log. `LINE_CONTENT=message` ships only the part a human reads. Raw is the
default because it is lossless and because `|= "some string"` then behaves the
way people expect; `message` is the one to pick when volume is the constraint.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from .logtypes import DISPATCHER_LEVELS, LogType, normalise_level

_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

_NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    """One parsed log line.

    `timestamp_ns` of None means "no usable event time in this line"; the caller
    stamps it at ingestion. That is a normal outcome, not a failure - an
    unmatched line has no timestamp to extract.
    """

    message: str
    timestamp_ns: int | None = None
    level: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


# --- Timestamp parsing -------------------------------------------------------
#
# The two hot formats are hand-parsed by offset rather than through
# `datetime.strptime`. That is not premature: the access and request logs are
# the high-volume pair - the sample author-tier day held 22k and 46k lines while
# the error log held 3k - and strptime costs roughly an order of magnitude more
# per call than slicing a fixed-width string. The rarer formats use strptime,
# where clarity is worth more than the microseconds.


def parse_clf_timestamp(text: str) -> int | None:
    """Parse `24/Jul/2026:00:00:04 +0000` to epoch nanoseconds.

    The Apache common-log-format stamp, used by aemaccess, aemrequest,
    aemdispatcher and aemhttpdaccess.
    """
    try:
        epoch = calendar.timegm(
            (
                int(text[7:11]),
                _MONTHS[text[3:6]],
                int(text[0:2]),
                int(text[12:14]),
                int(text[15:17]),
                int(text[18:20]),
                0,
                0,
                0,
            )
        )
        # An absent or malformed offset is treated as UTC rather than failing the
        # whole parse: AEM Cloud Service emits +0000 and the timestamp is still
        # worth having if the field is truncated.
        if len(text) >= 26 and text[21] in "+-":
            offset = int(text[22:24]) * 3600 + int(text[24:26]) * 60
            epoch += -offset if text[21] == "+" else offset
    except (ValueError, KeyError, IndexError):
        return None
    return epoch * _NANOS_PER_SECOND


def parse_aem_timestamp(text: str, *, utc_offset_seconds: int = 0) -> int | None:
    """Parse `24.07.2026 00:00:00.002` to epoch nanoseconds.

    The AEM Java error-log stamp. It carries **no timezone**. AEM Cloud Service
    writes UTC, which is why the default offset is zero, but a self-hosted AEM
    configured to local time would be silently shifted - hence `LOG_UTC_OFFSET`
    on the handler rather than a hardcoded assumption.
    """
    try:
        epoch = calendar.timegm(
            (
                int(text[6:10]),
                int(text[3:5]),
                int(text[0:2]),
                int(text[11:13]),
                int(text[14:16]),
                int(text[17:19]),
                0,
                0,
                0,
            )
        )
        millis = int(text[20:23]) if len(text) >= 23 and text[19] == "." else 0
    except (ValueError, IndexError):
        return None
    return (epoch - utc_offset_seconds) * _NANOS_PER_SECOND + millis * 1_000_000


def parse_iso_timestamp(text: str) -> int | None:
    """Parse the CDN log's `2026-07-24T00:00:04+0000`.

    `fromisoformat` accepts a `+0000` offset without a colon on Python 3.11 and
    later, which is why this is one call rather than a format-string cascade.
    """
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(moment.timestamp() * _NANOS_PER_SECOND)


def parse_httpd_error_timestamp(text: str, *, utc_offset_seconds: int = 0) -> int | None:
    """Parse Apache's `Fri Jul 17 02:19:48.093820 2020`.

    Also timezone-free, and also UTC on AEM Cloud Service.
    """
    try:
        moment = datetime.strptime(text, "%a %b %d %H:%M:%S.%f %Y")  # noqa: DTZ007
    except ValueError:
        return None
    epoch = calendar.timegm(moment.timetuple()) - utc_offset_seconds
    return epoch * _NANOS_PER_SECOND + moment.microsecond * 1000


# --- Shared field helpers ----------------------------------------------------


def status_class(status: str) -> str | None:
    """`404` -> `4xx`.

    Present as its own metadata field so the error-ratio and availability panels
    are a plain equality match. Deriving it at query time needs a regex or a
    range comparison on a string, both of which are slower and read worse in a
    panel a customer is meant to copy.
    """
    if len(status) == 3 and status.isdigit() and status[0] in "12345":
        return f"{status[0]}xx"
    return None


def _put(target: dict[str, str], key: str, value: str | None) -> None:
    """Set a metadata field, skipping anything empty.

    The CDN log writes `""` for six of its twenty-five fields on a typical line
    (`rules`, `alerts`, `debug`, `sample`, `res_age`, `res_ctype` on a synthetic
    response). Shipping those costs structured-metadata bytes on every single
    entry and tells a query nothing, and `| rules = ""` is not a filter anyone
    writes.
    """
    if value:
        target[key] = value


def _split_request(request: str) -> tuple[str | None, str | None, str | None]:
    """Split `GET /path?q=1 HTTP/1.1` into method, path and protocol.

    The query string stays on `path`. Stripping it would lose the only signal
    that distinguishes a cache-busting request from a cacheable one, and it is
    metadata rather than a label so its cardinality costs nothing in streams.
    """
    parts = request.split(" ")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return parts[0], parts[1], None
    return (None, request, None) if request else (None, None, None)


def _unmatched(line: str, log_type: LogType) -> ParsedRecord:
    return ParsedRecord(
        message=line,
        metadata={"parse_status": "unmatched", "expected_log_type": str(log_type)},
    )


# --- aemaccess / aemhttpdaccess ----------------------------------------------
#
# Adobe documents this as `NodeID - User Date Time "METHOD URL Protocol" Status
# Bytes "Referrer" "UserAgent"`, where the `-` in their example is the client
# address field rather than a literal. Real output confirms the address is there:
#
#   cm-p12345-e67890-aem-author-abc123def-x1y2z 203.0.113.29 - \
#     24/Jul/2026:00:00:04 +0000 "HEAD /systemready HTTP/1.1" 200 - "-" "Java/21"

_ACCESS_RE = re.compile(
    r"^(?P<node>\S+) (?P<client_ip>\S+) (?P<user>\S+) "
    r"\[?(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]? "
    r'"(?P<request>[^"]*)" (?P<status>\S+) (?P<bytes>\S+)'
    r'(?: "(?P<referer>[^"]*)" "(?P<agent>[^"]*)")?'
)


def parse_access(line: str, log_type: LogType = LogType.AEM_ACCESS) -> ParsedRecord:
    match = _ACCESS_RE.match(line)
    if match is None:
        return _unmatched(line, log_type)
    groups = match.groupdict()
    method, path, protocol = _split_request(groups["request"] or "")
    metadata: dict[str, str] = {}
    _put(metadata, "node_id", groups["node"])
    _put(metadata, "client_ip", groups["client_ip"])
    # `-` is Apache's "no value". Kept out of metadata entirely rather than
    # stored as a literal dash, so `| remote_user != ""` means what it looks like.
    _put(metadata, "remote_user", None if groups["user"] == "-" else groups["user"])
    _put(metadata, "method", method)
    _put(metadata, "path", path)
    _put(metadata, "protocol", protocol)
    _put(metadata, "status", groups["status"])
    _put(metadata, "status_class", status_class(groups["status"] or ""))
    _put(metadata, "bytes_sent", None if groups["bytes"] == "-" else groups["bytes"])
    _put(metadata, "referer", None if groups["referer"] in (None, "-") else groups["referer"])
    _put(metadata, "user_agent", None if groups["agent"] in (None, "-") else groups["agent"])
    return ParsedRecord(
        message=groups["request"] or line,
        timestamp_ns=parse_clf_timestamp(groups["ts"] or ""),
        metadata=metadata,
    )


def parse_httpd_access(line: str) -> ParsedRecord:
    """The Apache access log on the publish tier.

    Same field order as aemaccess, so the same parser - but the log type differs
    and therefore so does the label, which is why this is a named function
    rather than a shared default argument at the call site.
    """
    return parse_access(line, LogType.AEM_HTTPD_ACCESS)


# --- aemerror ----------------------------------------------------------------
#
#   24.07.2026 00:00:00.002 [node] *INFO* [thread] logger message

# The thread field nests brackets, which a `[^\]]*` group silently truncates -
# it left 41 of 250 real lines unparsed. AEM writes thread names like
# `[DocumentDiscoveryLiteService-BackgroundWorker-[4]]` and, for its repository
# API, `[[<uuid>][<app>][<tenant>]]`.
#
# So the thread group matches one level of nesting explicitly:
# `(?:[^\[\]]|\[[^\[\]]*\])*`. A greedy `.*` would be the obvious alternative
# and is wrong - it backtracks from the end of the line, so a message that
# itself contains `] ` (`ServiceEvent [Foo] REGISTERED`, which AEM emits
# constantly) swallows the logger into the thread field.
_ERROR_RE = re.compile(
    r"^(?P<ts>\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\.\d{3}) "
    r"\[(?P<node>[^\]]*)\] "
    r"\*?(?P<level>[A-Za-z]+)\*? "
    r"\[(?P<thread>(?:[^\[\]]|\[[^\[\]]*\])*)\] "
    r"(?P<logger>\S+)(?: (?P<message>.*))?$"
)

# A Java exception class at the head of a message or a stack trace line. Worth
# extracting because "which exceptions are firing" is the first question on an
# error dashboard, and the answer is otherwise buried in free text.
_EXCEPTION_RE = re.compile(
    r"\b(?P<exception>(?:[a-z][a-z0-9_]*\.){2,}[A-Z][A-Za-z0-9_]*"
    r"(?:Exception|Error|Throwable|Fault))\b"
)


def parse_error(line: str, *, utc_offset_seconds: int = 0) -> ParsedRecord:
    match = _ERROR_RE.match(line)
    if match is None:
        # A stack trace continuation line - `\tat com.foo.Bar.method(Bar.java:42)`
        # - has none of the leading fields. It is a real part of the preceding
        # event, so it ships with what can be recovered from it rather than being
        # labelled unmatched, which would make the unmatched-line panel useless
        # on any log containing a stack trace.
        stripped = line.strip()
        if stripped.startswith(("at ", "Caused by:", "... ", "Suppressed:")):
            trace_metadata: dict[str, str] = {"parse_status": "stacktrace"}
            found = _EXCEPTION_RE.search(line)
            if found:
                trace_metadata["exception"] = found.group("exception")
            return ParsedRecord(message=line, metadata=trace_metadata)
        return _unmatched(line, LogType.AEM_ERROR)

    groups = match.groupdict()
    message = groups["message"] or ""
    metadata: dict[str, str] = {}
    _put(metadata, "node_id", groups["node"])
    _put(metadata, "thread", groups["thread"])
    _put(metadata, "logger", groups["logger"])
    found = _EXCEPTION_RE.search(message)
    if found:
        _put(metadata, "exception", found.group("exception"))
    return ParsedRecord(
        message=f"{groups['logger']} {message}".strip(),
        timestamp_ns=parse_aem_timestamp(groups["ts"] or "", utc_offset_seconds=utc_offset_seconds),
        level=normalise_level(groups["level"] or ""),
        metadata=metadata,
    )


# --- aemrequest --------------------------------------------------------------
#
# Two lines per request, joined by the bracketed request id:
#
#   24/Jul/2026:00:00:04 +0000 [17722] -> HEAD /systemready HTTP/1.1 [node]
#   24/Jul/2026:00:00:04 +0000 [17722] <- 200 application/json 2ms [node]
#
# The response line carries the duration, which is the single most useful number
# in any AEM log, and the request line carries the method and path it belongs to.
# Neither line is useful alone, which is what `correlate.py` exists to fix.

_REQUEST_RE = re.compile(
    r"^(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}) "
    r"\[(?P<request_id>[^\]]*)\] "
    r"(?P<direction>->|<-) "
    r"(?P<rest>.*?)"
    r"(?: \[(?P<node>[^\]]*)\])?$"
)

_RESPONSE_RE = re.compile(r"^(?P<status>\d{3}) (?P<content_type>\S*) (?P<duration>\d+)ms$")


def parse_request(line: str) -> ParsedRecord:
    match = _REQUEST_RE.match(line)
    if match is None:
        return _unmatched(line, LogType.AEM_REQUEST)
    groups = match.groupdict()
    rest = (groups["rest"] or "").strip()
    metadata: dict[str, str] = {}
    _put(metadata, "node_id", groups["node"])
    _put(metadata, "request_id", groups["request_id"])

    if groups["direction"] == "->":
        metadata["direction"] = "request"
        method, path, protocol = _split_request(rest)
        _put(metadata, "method", method)
        _put(metadata, "path", path)
        _put(metadata, "protocol", protocol)
    else:
        metadata["direction"] = "response"
        response = _RESPONSE_RE.match(rest)
        if response is None:
            # A response line in a shape not seen in the sample. Shipped with the
            # direction still set, because knowing a response happened is worth
            # more than nothing.
            metadata["parse_status"] = "response-unmatched"
        else:
            _put(metadata, "status", response.group("status"))
            _put(metadata, "status_class", status_class(response.group("status")))
            _put(metadata, "content_type", response.group("content_type").split(";")[0] or None)
            # Milliseconds, as AEM writes it. Named with its unit because
            # `unwrap duration_ms` in a panel query is then self-describing, and
            # a unitless `duration` invites someone to read it as seconds.
            _put(metadata, "duration_ms", response.group("duration"))

    return ParsedRecord(
        message=rest or line,
        timestamp_ns=parse_clf_timestamp(groups["ts"] or ""),
        metadata=metadata,
    )


# --- aemdispatcher -----------------------------------------------------------
#
#   [17/Jul/2020:23:48:06 +0000] [I] [node] "GET /path" 200 12ms [farm] [HIT] "host"
#
# Adobe documents the field order but not a strict grammar, and dispatcher
# builds differ in which of the trailing bracketed fields they emit. So the
# leading fields are matched strictly and the tail is matched leniently, rather
# than one rigid pattern that silently fails on a variant.

_DISPATCHER_RE = re.compile(
    r"^\[(?P<ts>\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\] "
    r"\[(?P<level>[A-Za-z])\] "
    r"\[(?P<node>[^\]]*)\] "
    r"(?P<rest>.*)$"
)

_DISPATCHER_TAIL_RE = re.compile(
    r'^"(?P<request>[^"]*)" (?P<status>\d{3})(?: (?P<duration>\d+)ms)?'
    r"(?: \[(?P<farm>[^\]]*)\])?"
    r"(?: \[(?P<cache>[^\]]*)\])?"
    r'(?: "(?P<host>[^"]*)")?'
)


def parse_dispatcher(line: str) -> ParsedRecord:
    match = _DISPATCHER_RE.match(line)
    if match is None:
        return _unmatched(line, LogType.AEM_DISPATCHER)
    groups = match.groupdict()
    metadata: dict[str, str] = {}
    _put(metadata, "node_id", groups["node"])

    tail = _DISPATCHER_TAIL_RE.match((groups["rest"] or "").strip())
    if tail is None:
        # Dispatcher also logs plain diagnostic messages with no request at all.
        # Those are legitimate lines, not parse failures.
        metadata["parse_status"] = "message-only"
    else:
        method, path, protocol = _split_request(tail.group("request") or "")
        _put(metadata, "method", method)
        _put(metadata, "path", path)
        _put(metadata, "protocol", protocol)
        _put(metadata, "status", tail.group("status"))
        _put(metadata, "status_class", status_class(tail.group("status") or ""))
        _put(metadata, "duration_ms", tail.group("duration"))
        _put(metadata, "farm", tail.group("farm"))
        _put(metadata, "cache_status", tail.group("cache"))
        _put(metadata, "host", tail.group("host"))

    return ParsedRecord(
        message=(groups["rest"] or "").strip() or line,
        timestamp_ns=parse_clf_timestamp(groups["ts"] or ""),
        level=DISPATCHER_LEVELS.get((groups["level"] or "").upper()),
        metadata=metadata,
    )


# --- aemhttpderror -----------------------------------------------------------
#
#   Fri Jul 17 02:19:48.093820 2020 [mpm_worker:notice] [pid 1:tid 1402721] [node] message

_HTTPD_ERROR_RE = re.compile(
    r"^\[?(?P<ts>\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2}\.\d+ \d{4})\]? "
    r"\[(?P<module>[^\]:]*):(?P<level>[^\]]*)\]"
    r"(?: \[pid (?P<pid>\d+)(?::tid (?P<tid>\d+))?\])?"
    r"(?: \[(?P<node>[^\]]*)\])?"
    r"(?: (?P<message>.*))?$"
)


def parse_httpd_error(line: str, *, utc_offset_seconds: int = 0) -> ParsedRecord:
    match = _HTTPD_ERROR_RE.match(line)
    if match is None:
        return _unmatched(line, LogType.AEM_HTTPD_ERROR)
    groups = match.groupdict()
    metadata: dict[str, str] = {}
    _put(metadata, "node_id", groups["node"])
    _put(metadata, "module", groups["module"])
    _put(metadata, "pid", groups["pid"])
    _put(metadata, "tid", groups["tid"])
    return ParsedRecord(
        message=groups["message"] or line,
        timestamp_ns=parse_httpd_error_timestamp(
            groups["ts"] or "", utc_offset_seconds=utc_offset_seconds
        ),
        level=normalise_level(groups["level"] or ""),
        metadata=metadata,
    )


# --- aemcdn ------------------------------------------------------------------
#
# JSON Lines. Every field is renamed to the same vocabulary the other parsers
# use - `cli_ip` becomes `client_ip`, `url` becomes `path`, `req_ua` becomes
# `user_agent` - so one dashboard panel can span the CDN and the AEM access log
# without a per-log-type query. Adobe's names are kept only where there is no
# equivalent elsewhere (`pop`, `cache`, `aem_tenant`).

_CDN_FIELDS: tuple[tuple[str, str], ...] = (
    ("cli_ip", "client_ip"),
    ("cli_country", "country"),
    ("cli_region", "region"),
    ("rid", "request_id"),
    ("req_ua", "user_agent"),
    ("host", "host"),
    ("url", "path"),
    ("method", "method"),
    ("res_ctype", "content_type"),
    ("cache", "cache_status"),
    ("pop", "pop"),
    ("res_age", "response_age_s"),
    ("rules", "waf_rules"),
    ("alerts", "waf_alerts"),
    ("aem_tenant", "aem_tenant"),
    ("aem_envKind", "aem_env_kind"),
)

# Latency, renamed with explicit units. Adobe's `ttfb` and `ttlb` are
# milliseconds; a panel using `unwrap ttfb_ms` says so, and one using `unwrap
# ttfb` leaves the reader to guess.
_CDN_DURATIONS: tuple[tuple[str, str], ...] = (("ttfb", "ttfb_ms"), ("ttlb", "ttlb_ms"))


def parse_cdn(line: str) -> ParsedRecord:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return _unmatched(line, LogType.AEM_CDN)
    if not isinstance(record, dict):
        return _unmatched(line, LogType.AEM_CDN)

    metadata: dict[str, str] = {}
    for source, target in _CDN_FIELDS:
        value = record.get(source)
        if isinstance(value, str):
            _put(metadata, target, value)
        elif value is not None and not isinstance(value, bool):
            _put(metadata, target, str(value))

    for source, target in _CDN_DURATIONS:
        value = record.get(source)
        if isinstance(value, int | float) and not isinstance(value, bool):
            _put(metadata, target, str(value))

    status = record.get("status")
    if status is not None:
        _put(metadata, "status", str(status))
        _put(metadata, "status_class", status_class(str(status)))

    # `ddos` is a real boolean rather than a string, and it is the one field
    # where False is worth keeping: a panel filtering `| ddos = "true"` needs the
    # absent case to mean "not flagged", not "field missing".
    ddos = record.get("ddos")
    if isinstance(ddos, bool):
        metadata["ddos"] = "true" if ddos else "false"

    timestamp = record.get("timestamp")
    return ParsedRecord(
        # The raw JSON is kept as the message so `| json` still works for anyone
        # who wants a field this parser does not promote. Every field is already
        # in metadata, so a query never needs to - but a customer's own field
        # added by a future Adobe release would otherwise be unreachable.
        message=line,
        timestamp_ns=parse_iso_timestamp(timestamp) if isinstance(timestamp, str) else None,
        metadata=metadata,
    )


# --- Dispatch ----------------------------------------------------------------

Parser = Callable[[str], ParsedRecord]


def parser_for(log_type: LogType, *, utc_offset_seconds: int = 0) -> Parser:
    """Return the parser for a log type.

    `UNKNOWN` gets a passthrough rather than an error: an unclassified object
    still ships, line for line, with ingestion timestamps. That is the
    degradation this example promises when a customer's key layout does not match
    the configured pattern and the content sniffer finds nothing.
    """
    match log_type:
        case LogType.AEM_ACCESS:
            return parse_access
        case LogType.AEM_HTTPD_ACCESS:
            return parse_httpd_access
        case LogType.AEM_ERROR:
            return lambda line: parse_error(line, utc_offset_seconds=utc_offset_seconds)
        case LogType.AEM_REQUEST:
            return parse_request
        case LogType.AEM_DISPATCHER:
            return parse_dispatcher
        case LogType.AEM_HTTPD_ERROR:
            return lambda line: parse_httpd_error(line, utc_offset_seconds=utc_offset_seconds)
        case LogType.AEM_CDN:
            return parse_cdn
        case _:
            return lambda line: ParsedRecord(message=line)
