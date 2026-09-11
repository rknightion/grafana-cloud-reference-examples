"""The Loki label contract for this example.

Its own module, importable without any configuration, because `handler.py`
builds a Loki client and reads Secrets Manager at import time - so anything that
needs only the label rules (the tests, the local dev runner) must not have to go
through it.

## The rule

A label partitions Loki streams, and the stream count is the **product** of
every label's cardinality. So a label is only ever a deployment coordinate:
which environment, which tier, which log type, and a severity where AEM assigned
one. Worst realistic case is `environments x tiers x log types x levels`, which
for ten environments is a few hundred streams.

Everything else - path, client address, request id, node id, object key, status,
method, duration - goes into structured metadata. Loki indexes that per entry
without creating a stream, and `sum by (...)`, `count_over_time` and `unwrap` all
read it directly, so a dashboard aggregates on it with no `| json` or `| regexp`
stage. That is the whole reason this example exists rather than a raw shipper.

`status` is the one that looks like it should be a label and must not be. It is
a small set on its own, but it multiplies every other label present, and it is
never queried without also slicing by path or method - which are unbounded.
"""

from __future__ import annotations

from .logtypes import LEVELLED_TYPES, LogType, Tier

# Label names, in one place, so the dashboards, the README and the code cannot
# drift apart. Changing one of these renames a label in Loki and orphans every
# existing stream, so it is a breaking change for a deployment.
LABEL_SERVICE = "service_name"
LABEL_LOG_TYPE = "log_type"
LABEL_TIER = "aem_tier"
LABEL_PROGRAM = "aem_program_id"
LABEL_ENV_ID = "aem_env_id"
LABEL_ENV_TYPE = "aem_env_type"
LABEL_LEVEL = "level"

ALL_LABELS = (
    LABEL_SERVICE,
    LABEL_LOG_TYPE,
    LABEL_TIER,
    LABEL_PROGRAM,
    LABEL_ENV_ID,
    LABEL_ENV_TYPE,
    LABEL_LEVEL,
)


# Maps an HTTP status class onto a log level, for the four log types where AEM
# assigns no severity of its own: aemaccess, aemhttpdaccess, aemrequest, aemcdn.
#
# This value goes into structured metadata as `detected_level`, **never into the
# `level` label**. The distinction is the point: `level` is what AEM wrote, and
# `detected_level` is what this example inferred, so nobody has to guess which
# they are looking at. It also costs no streams.
#
# It is worth setting rather than leaving blank because Grafana Cloud fills
# `detected_level` in itself when the field is absent, and for these formats it
# cannot tell - every one of those entries arrived carrying
# `detected_level="unknown"`, verified on a live tenant. A derived value is more
# use than that, and it makes the log-level colouring and the Logs Drilldown
# level filter work on an access log.
STATUS_CLASS_LEVELS = {"5xx": "error", "4xx": "warn", "3xx": "info", "2xx": "info", "1xx": "info"}


def detected_level_for(metadata: dict[str, str]) -> str | None:
    """Infer a level for a log type that carries no severity. None if unknowable."""
    return STATUS_CLASS_LEVELS.get(metadata.get("status_class", ""))


def build_labels(
    *,
    service_name: str,
    log_type: LogType,
    tier: Tier,
    coordinates: dict[str, str],
    level: str | None = None,
    default_program_id: str = "",
    default_env_id: str = "",
    default_env_type: str = "",
) -> dict[str, str]:
    """Build the stream labels for one line.

    A coordinate found in the object key wins over its configured default,
    because the key describes the object and the default describes the
    deployment.

    An absent coordinate means the label is **omitted**, not set to a
    placeholder. A label whose value is "unknown" still creates a stream and
    still reads as data on a dashboard, which is worse than its absence.
    """
    labels = {
        LABEL_SERVICE: service_name,
        LABEL_LOG_TYPE: str(log_type),
        LABEL_TIER: str(tier),
    }
    for name, from_key_value, default in (
        (LABEL_PROGRAM, coordinates.get("program_id"), default_program_id),
        (LABEL_ENV_ID, coordinates.get("env_id"), default_env_id),
        (LABEL_ENV_TYPE, coordinates.get("env_type"), default_env_type),
    ):
        value = (from_key_value or default or "").strip()
        if value:
            labels[name] = value

    # Only for a log type AEM assigned a severity to. Deriving a level from an
    # access log's status code would put a fabricated field into a label, where
    # it is indistinguishable from one AEM actually wrote.
    if level and log_type in LEVELLED_TYPES:
        labels[LABEL_LEVEL] = level
    return labels
