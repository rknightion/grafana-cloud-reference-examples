"""Work out which log type, tier and environment an S3 object holds.

Two independent routes, in this order:

1. **The object key**, matched against a configurable regex with named groups.
   This is the reliable route when it works, because it identifies a whole object
   before a byte is read.
2. **The first content line**, shape-matched. A fallback, used only for the log
   type, and only when the key did not supply one.

The key pattern is configurable rather than fixed because Adobe's documented S3
layout is not something this example can pin down: the log-forwarding
documentation specifies the *Azure Blob Storage* CDN filename format
(`YYYY-MM-DDThhss.sss-uniqueid.log`) and lists S3 CDN forwarding as a future
capability, while the AEM and Dispatcher logs go to a prefix the customer
chooses in Cloud Manager. Hardcoding one shape would be a guess that fails
silently by putting every line in the `unknown` stream.

So: `KEY_PATTERN` is an ERE with named groups, defaulting to the shape of the
files Adobe's own download produces (`author_aemaccess_2026-07-24.log`), which
matches an object stored under any prefix because the pattern is searched rather
than anchored.
"""

from __future__ import annotations

import json
import re

from .logtypes import LogType, Tier

# Searched, not anchored, so any prefix in front of the basename is ignored:
# `logs/2026/07/24/author_aemaccess_2026-07-24.log` matches on the basename
# alone. `(?P<env_type>...)` is absent here on purpose - the filename carries no
# environment - so it falls back to `AEM_ENV_TYPE`.
DEFAULT_KEY_PATTERN = (
    r"(?P<tier>author|publish|preview|dispatcher)[._-]"
    r"(?P<log_type>aemaccess|aemerror|aemrequest|aemdispatcher"
    r"|aemhttpdaccess|aemhttpderror|aemcdn|cdn)"
)

# Recognised group names. Anything else in the pattern is ignored rather than
# rejected, so a customer can use non-capturing context or extra groups for
# readability without this failing on them.
KEY_GROUPS = ("program_id", "env_id", "env_type", "tier", "log_type")

# Adobe's download names the CDN file `cdn`; the forwarding configuration and
# the documentation call the type `aemcdn`. Both map to one label value, because
# two names for one log type is two streams for one thing.
_LOG_TYPE_ALIASES = {"cdn": LogType.AEM_CDN, "httpdaccess": LogType.AEM_HTTPD_ACCESS}


class ClassificationError(ValueError):
    """The configured key pattern is unusable."""


def compile_key_pattern(pattern: str) -> re.Pattern[str]:
    """Compile `KEY_PATTERN`, rejecting one that captures nothing useful.

    A pattern with no recognised group compiles fine and then matches every
    object while extracting nothing, which is indistinguishable from working.
    Failing here means a bad pattern is a cold-start error with a clear message
    rather than a month of logs in the `unknown` stream.
    """
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ClassificationError(f"KEY_PATTERN is not a valid regex: {exc}") from exc
    named = set(compiled.groupindex) & set(KEY_GROUPS)
    if not named:
        raise ClassificationError(
            f"KEY_PATTERN has no recognised named group. Use at least one of "
            f"{', '.join(KEY_GROUPS)} - for example "
            f"'(?P<tier>author|publish)_(?P<log_type>aemaccess|aemerror)'. "
            f"A pattern that captures nothing matches every object and extracts "
            f"nothing, which looks exactly like working."
        )
    return compiled


def log_type_from_name(raw: str) -> LogType:
    token = raw.strip().lower()
    if token in _LOG_TYPE_ALIASES:
        return _LOG_TYPE_ALIASES[token]
    try:
        return LogType(token)
    except ValueError:
        return LogType.UNKNOWN


def tier_from_name(raw: str) -> Tier:
    try:
        return Tier(raw.strip().lower())
    except ValueError:
        return Tier.UNKNOWN


def from_key(key: str, pattern: re.Pattern[str]) -> dict[str, str]:
    """Extract whatever the key pattern captures. Missing groups are just absent.

    `re.Match.groupdict` includes every group in the pattern; only the recognised
    ones with a non-empty value are returned, so a caller can treat the result as
    "what we know from the key".
    """
    match = pattern.search(key)
    if match is None:
        return {}
    captured = match.groupdict()
    return {name: captured[name] for name in KEY_GROUPS if captured.get(name)}


# --- Content sniffing --------------------------------------------------------
#
# Ordered most-specific first. `aemaccess` and `aemhttpdaccess` are
# indistinguishable by content - identical field order, and the only difference
# is which process wrote them - so the sniffer returns `aemaccess` and the key
# pattern is the only way to tell them apart. Documented rather than guessed at.

_SNIFFERS: tuple[tuple[LogType, re.Pattern[str]], ...] = (
    (
        LogType.AEM_ERROR,
        re.compile(r"^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\.\d{3} \["),
    ),
    (
        LogType.AEM_REQUEST,
        re.compile(r"^\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4} \[[^\]]*\] (?:->|<-) "),
    ),
    (
        LogType.AEM_DISPATCHER,
        re.compile(r"^\[\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\] \[[A-Za-z]\] "),
    ),
    (
        LogType.AEM_HTTPD_ERROR,
        re.compile(r"^\[?\w{3} \w{3} +\d{1,2} \d{2}:\d{2}:\d{2}\.\d+ \d{4}\]? \[[^\]:]*:"),
    ),
    (
        LogType.AEM_ACCESS,
        re.compile(r"^\S+ \S+ \S+ \[?\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\]? \""),
    ),
)

# A CDN line is JSON, and these are the fields that make it an AEM CDN line
# rather than any other JSON. Two of the five is enough: `aem_tenant` is absent
# on some Adobe releases, and requiring all of them would fail closed.
_CDN_MARKERS = ("cli_ip", "aem_tenant", "rid", "pop", "aem_envKind")


def sniff_log_type(line: str) -> LogType:
    """Identify a log type from one line. `UNKNOWN` when nothing matches."""
    stripped = line.strip()
    if not stripped:
        return LogType.UNKNOWN

    if stripped.startswith("{"):
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            return LogType.UNKNOWN
        if isinstance(record, dict):
            present = sum(1 for marker in _CDN_MARKERS if marker in record)
            if present >= 2:
                return LogType.AEM_CDN
        return LogType.UNKNOWN

    for log_type, pattern in _SNIFFERS:
        if pattern.match(stripped):
            return log_type
    return LogType.UNKNOWN
