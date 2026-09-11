"""Shared building blocks for the Python reference examples.

Import from here, not from the submodules, so an example's imports stay stable
when internals move.

The core (config, log, loki, batching, errors) depends on the standard library
only. AWS helpers live in ``grafana_cloud_common.aws`` and are imported
separately, so a non-AWS example never pulls boto3 in.
"""

from .batching import StreamBatcher
from .config import LokiConfig, normalise_push_url
from .errors import (
    ConfigError,
    GrafanaCloudError,
    ParseError,
    PermanentError,
    RetryableError,
)
from .log import FieldLogger, configure, get_logger
from .loki import (
    BANNED_LABEL_NAMES,
    MAX_LABELS_PER_STREAM,
    LogEntry,
    LokiClient,
    Stream,
    StreamBatch,
    group_by_labels,
    validate_labels,
)

__all__ = [
    "BANNED_LABEL_NAMES",
    "MAX_LABELS_PER_STREAM",
    "ConfigError",
    "FieldLogger",
    "GrafanaCloudError",
    "LogEntry",
    "LokiClient",
    "LokiConfig",
    "ParseError",
    "PermanentError",
    "RetryableError",
    "Stream",
    "StreamBatch",
    "StreamBatcher",
    "configure",
    "get_logger",
    "group_by_labels",
    "normalise_push_url",
    "validate_labels",
]
