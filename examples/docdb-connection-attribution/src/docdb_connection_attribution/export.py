"""Export one snapshot as OTLP gauges.

A snapshot per run, with no staleness marker when a series disappears: query it with
``last_over_time(...[2m])`` (two schedule intervals) or a closed connection keeps showing for
the 5-minute Prometheus lookback. The shipped dashboard does.
"""

from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Iterable

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
from opentelemetry.sdk.resources import Resource

from grafana_cloud_common import RetryableError

from .attribution import SeriesKey, UserRole

SERVICE_NAME = "docdb-connection-attribution"


def build_resource(cluster: str) -> Resource:
    """The exporting identity, fixed per cluster.

    Set explicitly because recent OTel SDKs fill in a random service.instance.id per process,
    which the Grafana Cloud OTLP gateway turns into an `instance` label: every Lambda cold start
    would begin a new set of series and double-count across the query window.
    """
    return Resource.create(
        {"service.name": SERVICE_NAME, "service.instance.id": f"{SERVICE_NAME}/{cluster}"}
    )


def export_snapshot(
    *,
    url: str,
    tenant_id: str,
    token: str,
    cluster: str,
    counts: Counter[SeriesKey],
    roles: list[UserRole],
    audit_events: int,
    timeout_s: float = 10.0,
) -> None:
    def connections(_: CallbackOptions) -> Iterable[Observation]:
        for key, n in counts.items():
            # Everything a dashboard filters on goes on the data point. Resource attributes
            # land on target_info only, not on each series.
            attrs = {
                "docdb.cluster": cluster,
                "docdb.instance": key.instance,
                "docdb.user": key.user,
                "docdb.app_name": key.app,
                "docdb.connection.state": key.state,
            }
            if key.client_address is not None:
                attrs["client.address"] = key.client_address
            yield Observation(n, attrs)

    def user_role(_: CallbackOptions) -> Iterable[Observation]:
        for r in roles:
            yield Observation(
                1,
                {
                    "docdb.cluster": cluster,
                    "docdb.user": r.user,
                    "docdb.role": r.role,
                    "docdb.role_db": r.db,
                },
            )

    def audit(_: CallbackOptions) -> Iterable[Observation]:
        yield Observation(audit_events, {"docdb.cluster": cluster})

    reader = InMemoryMetricReader()
    provider = MeterProvider(resource=build_resource(cluster), metric_readers=[reader])
    meter = provider.get_meter(SERVICE_NAME)
    meter.create_observable_gauge(
        "docdb.connections.open",
        callbacks=[connections],
        unit="{connection}",
        description="Open DocumentDB connections by authenticated user, client app and state",
    )
    meter.create_observable_gauge(
        "docdb.user.role",
        callbacks=[user_role],
        unit="{role}",
        description="1 per role granted to each DocumentDB user; join on docdb_user",
    )
    meter.create_observable_gauge(
        "docdb.attributor.audit_events",
        callbacks=[audit],
        unit="{event}",
        description="Audit authenticate events read in this run",
    )
    data = reader.get_metrics_data()
    auth = base64.b64encode(f"{tenant_id}:{token}".encode()).decode()
    exporter = OTLPMetricExporter(
        endpoint=url, headers={"Authorization": f"Basic {auth}"}, timeout=timeout_s
    )
    try:
        if data is None or exporter.export(data) != MetricExportResult.SUCCESS:
            # The exporter logs the HTTP status itself. Raising makes the run fail visibly, so
            # the error alarm fires rather than the dashboard quietly going flat.
            raise RetryableError("OTLP metrics export to Grafana Cloud failed; see preceding log")
    finally:
        exporter.shutdown()
        provider.shutdown()
