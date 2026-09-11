"""Adobe Experience Manager Cloud Service logs to Grafana Cloud Loki.

The parsing, classification and correlation pieces are importable on their own
so the local development tooling under `dev/` can run them without AWS, and so
the tests can exercise a parser against a fixture line without a Lambda event.
"""

from __future__ import annotations

from .classify import (
    DEFAULT_KEY_PATTERN,
    ClassificationError,
    compile_key_pattern,
    from_key,
    log_type_from_name,
    sniff_log_type,
    tier_from_name,
)
from .correlate import CorrelationStats, RequestCorrelator
from .logtypes import LEVELLED_TYPES, LogType, Tier, normalise_level
from .parsers import ParsedRecord, parser_for, status_class

__all__ = [
    "DEFAULT_KEY_PATTERN",
    "LEVELLED_TYPES",
    "ClassificationError",
    "CorrelationStats",
    "LogType",
    "ParsedRecord",
    "RequestCorrelator",
    "Tier",
    "compile_key_pattern",
    "from_key",
    "log_type_from_name",
    "normalise_level",
    "parser_for",
    "sniff_log_type",
    "status_class",
    "tier_from_name",
]
