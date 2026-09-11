"""The seven log types Adobe Experience Manager Cloud Service forwards.

The names are Adobe's own, as used in the Cloud Manager log-forwarding
configuration, so a value here matches what a customer sees in their own setup
rather than something invented for this example.

`log_type` is a Loki *label*, so this set is also a promise about stream
cardinality: seven values, fixed, and it only grows when Adobe adds a log type.
"""

from __future__ import annotations

from enum import StrEnum


class LogType(StrEnum):
    """A log type AEM Cloud Service can forward."""

    AEM_ACCESS = "aemaccess"
    AEM_ERROR = "aemerror"
    AEM_REQUEST = "aemrequest"
    AEM_DISPATCHER = "aemdispatcher"
    AEM_HTTPD_ACCESS = "aemhttpdaccess"
    AEM_HTTPD_ERROR = "aemhttpderror"
    AEM_CDN = "aemcdn"
    # Not an Adobe log type. The value used when neither the object key nor the
    # content identifies one, so a line is still shipped - labelled honestly as
    # unclassified rather than guessed into the wrong stream.
    UNKNOWN = "unknown"


class Tier(StrEnum):
    """Which AEM service produced the log.

    `dispatcher` is included because Adobe's own forwarding configuration treats
    it as a tier alongside the three AEM services, even though it is the Apache
    layer in front of publish rather than an AEM instance.
    """

    AUTHOR = "author"
    PUBLISH = "publish"
    PREVIEW = "preview"
    DISPATCHER = "dispatcher"
    UNKNOWN = "unknown"


# Log types that carry a severity AEM itself assigned. Only these contribute a
# `level` label; attaching one to an access log would mean inventing a severity
# from a status code, which reads as data and is not.
LEVELLED_TYPES = frozenset({LogType.AEM_ERROR, LogType.AEM_DISPATCHER, LogType.AEM_HTTPD_ERROR})

# AEM's single-letter dispatcher levels, and the full words used elsewhere.
# Normalised to the lowercase words Grafana's log UI recognises, so the level
# colouring and the Logs Drilldown level filter both work without per-dashboard
# configuration.
DISPATCHER_LEVELS = {
    "E": "error",
    "W": "warn",
    "I": "info",
    "D": "debug",
    "T": "trace",
}

KNOWN_LEVELS = frozenset({"trace", "debug", "info", "warn", "error", "fatal"})


def normalise_level(raw: str) -> str | None:
    """Map any of AEM's severity spellings onto a lowercase canonical word.

    Returns None for something unrecognised rather than passing it through,
    because `level` is a label: one typo in one line would otherwise create a
    permanent stream.
    """
    word = raw.strip().strip("*").lower()
    if word in KNOWN_LEVELS:
        return word
    # AEM writes WARNING in some components and WARN in others.
    if word == "warning":
        return "warn"
    if word in {"err", "severe"}:
        return "error"
    if word in {"notice", "config", "fine", "finer", "finest"}:
        # Apache httpd and java.util.logging levels with no Loki equivalent.
        # Mapped down rather than dropped, so the line still gets a level.
        return "debug" if word in {"fine", "finer", "finest"} else "info"
    return None
