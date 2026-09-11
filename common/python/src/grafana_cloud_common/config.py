"""Environment-driven configuration, validated once at cold start.

Every example reads its config through here so the environment variable names,
the defaults and the failure messages are identical across the repo. A customer
who has deployed one example can read the next one's variables without relearning
them.

Config is resolved at import time in the handler module, not per invocation, so a
misconfiguration fails the first invocation loudly instead of silently degrading.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse, urlunparse

from .errors import ConfigError

# Loki rejects a push whose body exceeds the tenant's limit. Grafana Cloud's
# default server-side limit is larger than this, but a 4 MB uncompressed batch
# keeps a 128 MB Lambda comfortably inside memory while still amortising the
# round trip.
DEFAULT_BATCH_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_BATCH_MAX_LINES = 5_000
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_RETRIES = 5

_PUSH_PATH = "/loki/api/v1/push"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _required(name: str) -> str:
    value = _env(name)
    if value is None:
        raise ConfigError(f"{name} is required but unset or empty")
    return value


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive, got {parsed}")
    return parsed


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be positive, got {parsed}")
    return parsed


def _bool(name: str, *, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def normalise_push_url(endpoint: str) -> str:
    """Accept anything a customer is likely to paste and return the push URL.

    The Grafana Cloud UI shows several forms of the same thing - the stack's
    logs hostname, the base URL, and the full push URL. All three are normalised
    here rather than at every call site, because getting this wrong produces a
    404 that reads like an auth problem.
    """
    candidate = endpoint.strip()
    if not candidate:
        raise ConfigError("Loki endpoint is empty")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise ConfigError(f"Loki endpoint must be https, got {parsed.scheme!r}")
    if not parsed.netloc:
        raise ConfigError(f"Loki endpoint has no host: {endpoint!r}")

    path = parsed.path.rstrip("/")
    if path.endswith(_PUSH_PATH):
        pass
    elif path in {"", "/"}:
        path = _PUSH_PATH
    elif path.endswith("/loki"):
        path = f"{path}/api/v1/push"
    else:
        raise ConfigError(
            f"Loki endpoint path {parsed.path!r} is not recognised. "
            f"Pass the host, the base URL, or a URL ending in {_PUSH_PATH}."
        )

    return urlunparse(("https", parsed.netloc, path, "", "", ""))


def _parse_static_labels(raw: str | None) -> dict[str, str]:
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"LOKI_STATIC_LABELS must be a JSON object: {exc}") from exc
    if not isinstance(decoded, dict):
        raise ConfigError("LOKI_STATIC_LABELS must be a JSON object")
    labels: dict[str, str] = {}
    for key, value in decoded.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ConfigError("LOKI_STATIC_LABELS keys and values must both be strings")
        labels[key] = value
    return labels


@dataclass(frozen=True, slots=True)
class LokiConfig:
    """Everything needed to push to one Grafana Cloud Loki tenant."""

    push_url: str
    tenant_id: str
    credentials_secret_id: str | None
    token: str | None
    static_labels: dict[str, str] = field(default_factory=dict)
    batch_max_lines: int = DEFAULT_BATCH_MAX_LINES
    batch_max_bytes: int = DEFAULT_BATCH_MAX_BYTES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    compress: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> LokiConfig:
        """Build from environment variables, raising ConfigError on anything wrong.

        Either ``GRAFANA_CLOUD_CREDENTIALS_SECRET_ID`` or ``GRAFANA_CLOUD_LOKI_TOKEN``
        must be set. The secret is the documented path; the plain token exists for
        local testing and is never set by the shipped IaC.
        """
        secret_id = _env("GRAFANA_CLOUD_CREDENTIALS_SECRET_ID")
        token = _env("GRAFANA_CLOUD_LOKI_TOKEN")
        if secret_id is None and token is None:
            raise ConfigError(
                "Set GRAFANA_CLOUD_CREDENTIALS_SECRET_ID (the deployed path) or "
                "GRAFANA_CLOUD_LOKI_TOKEN (local testing only)"
            )

        return cls(
            push_url=normalise_push_url(_required("GRAFANA_CLOUD_LOKI_ENDPOINT")),
            tenant_id=_required("GRAFANA_CLOUD_LOKI_TENANT_ID"),
            credentials_secret_id=secret_id,
            token=token,
            static_labels=_parse_static_labels(_env("LOKI_STATIC_LABELS")),
            batch_max_lines=_int("LOKI_BATCH_MAX_LINES", DEFAULT_BATCH_MAX_LINES),
            batch_max_bytes=_int("LOKI_BATCH_MAX_BYTES", DEFAULT_BATCH_MAX_BYTES),
            request_timeout_seconds=_float(
                "LOKI_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
            ),
            max_retries=_int("LOKI_MAX_RETRIES", DEFAULT_MAX_RETRIES),
            compress=_bool("LOKI_COMPRESS", default=True),
            log_level=(_env("LOG_LEVEL") or "INFO").upper(),
        )
