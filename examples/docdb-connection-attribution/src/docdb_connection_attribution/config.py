"""Configuration, read once from the environment at cold start."""

from __future__ import annotations

import os
from dataclasses import dataclass

from grafana_cloud_common import ConfigError

DEFAULT_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _int(name: str, default: int, minimum: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    cluster_id: str
    docdb_credentials_secret_id: str
    ca_bundle_url: str
    audit_log_group: str
    audit_backfill_minutes: int
    table_name: str
    mapping_ttl_days: int
    otlp_endpoint: str
    grafana_credentials_secret_id: str
    grafana_tenant_id: str | None
    include_client_address: bool

    @property
    def otlp_metrics_url(self) -> str:
        return self.otlp_endpoint.rstrip("/") + "/v1/metrics"

    @classmethod
    def from_env(cls) -> Config:
        cluster_id = _required("DOCDB_CLUSTER_ID")
        endpoint = _required("GRAFANA_CLOUD_OTLP_ENDPOINT")
        if not endpoint.startswith("https://"):
            raise ConfigError("GRAFANA_CLOUD_OTLP_ENDPOINT must be an https:// URL")
        if endpoint.rstrip("/").endswith("/v1/metrics"):
            # The gateway base is what the stack details page shows. Accepting the
            # signal path as well would double it up when we append /v1/metrics.
            endpoint = endpoint.rstrip("/").removesuffix("/v1/metrics")
        tenant = (os.environ.get("GRAFANA_CLOUD_TENANT_ID") or "").strip() or None
        if tenant is not None and not tenant.isdigit():
            raise ConfigError("GRAFANA_CLOUD_TENANT_ID is the numeric stack (instance) id")
        return cls(
            cluster_id=cluster_id,
            docdb_credentials_secret_id=_required("DOCDB_CREDENTIALS_SECRET_ID"),
            ca_bundle_url=(os.environ.get("DOCDB_CA_BUNDLE_URL") or DEFAULT_CA_BUNDLE_URL).strip(),
            audit_log_group=(os.environ.get("AUDIT_LOG_GROUP") or "").strip()
            or f"/aws/docdb/{cluster_id}/audit",
            audit_backfill_minutes=_int("AUDIT_BACKFILL_MINUTES", 10080, 1),
            table_name=_required("ATTRIBUTION_TABLE"),
            mapping_ttl_days=_int("MAPPING_TTL_DAYS", 14, 1),
            otlp_endpoint=endpoint,
            grafana_credentials_secret_id=_required("GRAFANA_CLOUD_CREDENTIALS_SECRET_ID"),
            grafana_tenant_id=tenant,
            include_client_address=(os.environ.get("INCLUDE_CLIENT_ADDRESS") or "false")
            .strip()
            .lower()
            == "true",
        )
