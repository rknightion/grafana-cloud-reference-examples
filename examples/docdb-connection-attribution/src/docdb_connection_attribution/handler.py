"""Scheduled entry point: read new audit events, snapshot open connections, export.

Everything expensive is built at module scope, once per execution environment. Reserved
concurrency is 1 because the audit checkpoint in DynamoDB has a single writer.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any, cast

import boto3
from botocore.config import Config as BotoConfig
from pymongo.errors import PyMongoError

from grafana_cloud_common import ConfigError, configure, get_logger, sample_debug
from grafana_cloud_common.aws import CredentialProvider, LambdaContext

from .attribution import UNATTRIBUTED, SeriesKey, group_connections
from .audit import LogsSender, ingest
from .config import Config
from .docdb import (
    ConnectionLister,
    RdsSender,
    SecretsSender,
    docdb_credentials,
    fetch_ca_bundle,
    instance_endpoints,
)
from .export import export_snapshot
from .store import AttributionStore, DynamoDBSender

configure()
_LOG = get_logger(__name__)

CONFIG = Config.from_env()
# Standard retry mode backs off on FilterLogEvents throttling, whose quota is shared across
# the whole account and region.
_BOTO = BotoConfig(retries={"mode": "standard", "max_attempts": 5})
LOGS = cast(LogsSender, boto3.client("logs", config=_BOTO))
RDS = cast(RdsSender, boto3.client("rds", config=_BOTO))
STORE = AttributionStore(
    cast(DynamoDBSender, boto3.client("dynamodb", config=_BOTO)),
    CONFIG.table_name,
    CONFIG.mapping_ttl_days,
)
GRAFANA = CredentialProvider(CONFIG.grafana_credentials_secret_id)
_USER, _PASSWORD = docdb_credentials(
    cast(SecretsSender, boto3.client("secretsmanager", config=_BOTO)),
    CONFIG.docdb_credentials_secret_id,
)
LISTER = ConnectionLister(_USER, _PASSWORD, fetch_ca_bundle(CONFIG.ca_bundle_url))


def _lookup(instance: str, users: dict[tuple[str, str], str]) -> Callable[[str], str | None]:
    def find(client: str) -> str | None:
        return users.get((instance, client))

    return find


def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    sample_debug()
    ingested = ingest(
        LOGS,
        STORE,
        CONFIG.audit_log_group,
        backfill_minutes=CONFIG.audit_backfill_minutes,
        time_left_ms=context.get_remaining_time_in_millis,
    )

    counts: Counter[SeriesKey] = Counter()
    failed: list[str] = []
    for instance, host in instance_endpoints(RDS, CONFIG.cluster_id):
        try:
            conns = LISTER.open_connections(host)
        except PyMongoError as exc:
            # One unreachable instance should not blank the other instances' series.
            _LOG.error("could not list connections", instance=instance, cause=str(exc))
            LISTER.forget(host)
            failed.append(instance)
            continue
        users = STORE.lookup((instance, str(c.get("client"))) for c in conns if c.get("client"))
        counts += group_connections(
            instance,
            conns,
            _lookup(instance, users),
            include_client_address=CONFIG.include_client_address,
        )

    credential = GRAFANA.resolve()
    tenant = CONFIG.grafana_tenant_id or credential.tenant_id
    if not tenant:
        raise ConfigError(
            "no Grafana Cloud tenant id: put tenant_id in the credentials secret "
            "or set GRAFANA_CLOUD_TENANT_ID"
        )
    export_snapshot(
        url=CONFIG.otlp_metrics_url,
        tenant_id=tenant,
        token=credential.token,
        cluster=CONFIG.cluster_id,
        counts=counts,
        audit_events=ingested.events,
    )

    summary = {
        "connections": sum(counts.values()),
        "unattributed": sum(n for k, n in counts.items() if k.user == UNATTRIBUTED),
        "series": len(counts),
        "audit_events": ingested.events,
        "audit_complete": ingested.complete,
        "failed_instances": failed,
    }
    _LOG.info("snapshot exported", **summary)
    if failed:
        # After exporting what we could: the error alarm should still fire.
        raise RuntimeError(f"could not list connections on {', '.join(failed)}")
    return summary
