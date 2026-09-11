#!/usr/bin/env python3
"""Generate the two shipped dashboards as Grafana dashboard schema v2.

The committed artefact is the JSON under `dashboards/` - that is what a customer
imports. This generator exists because v2 JSON is not reviewable by hand: one
panel is about 60 lines of nested `kind`/`spec` envelopes, and a pull request
that changes a query shows up as an unreadable diff. The queries live here, in
one table, where a reviewer can actually read them.

Run through `just aem-dashboards`, which regenerates the JSON and fails if it
drifts from what is committed.

## Why schema v2

`dashboard.grafana.app/v2` is the `preferredVersion` on current Grafana (13.3),
and a v1 dashboard is converted to v2 on import - so v2 is the format with a
future and v1 is the one being migrated away from. The structural difference
that matters here: v2 separates `elements` (panels, addressed by name) from
`layout` (where they sit), so moving a panel does not touch its query.

## The query rule these dashboards demonstrate

**A structured metadata field goes after a `|`, never inside the `{}` stream
selector.** `{log_type="aemcdn", cache_status="HIT"}` returns nothing at all -
no error, just an empty result, because `{}` matches labels only. The working
form is `{log_type="aemcdn"} | cache_status="HIT"`. Every query below was run
against a live Loki tenant holding real parsed AEM data before being committed.

Filtering and aggregating on metadata needs no `| json` or `| regexp` stage,
which is the entire reason the parsers extract fields at write time.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DASHBOARDS_DIR = Path(__file__).resolve().parent.parent / "dashboards"

# Kept out of the queries so the label set is stated once. `$env_id` and
# `$tier` are dashboard variables; `service_name` is the one label a deployment
# is guaranteed to set.
BASE = 'service_name=~"$service", aem_env_id=~"$env_id", aem_tier=~"$tier"'
HTTP_TYPES = 'log_type=~"aemcdn|aemaccess|aemhttpdaccess"'


def panel(
    panel_id: int,
    title: str,
    viz: str,
    queries: list[tuple[str, str]],
    *,
    description: str = "",
    unit: str | None = None,
    options: dict[str, Any] | None = None,
    custom: dict[str, Any] | None = None,
    mappings: list[dict[str, Any]] | None = None,
    thresholds: dict[str, Any] | None = None,
    maximum: float | None = None,
    minimum: float | None = None,
    decimals: int | None = None,
    overrides: list[dict[str, Any]] | None = None,
    transformations: list[dict[str, Any]] | None = None,
    viz_version: str = "",
) -> dict[str, Any]:
    """Build one v2 Panel element.

    `queries` is a list of (expr, legend) pairs; the refId is assigned from the
    position, because a hand-maintained refId is a source of duplicate-refId
    errors that Grafana reports unhelpfully.
    """
    defaults: dict[str, Any] = {}
    if unit is not None:
        defaults["unit"] = unit
    if minimum is not None:
        defaults["min"] = minimum
    if maximum is not None:
        defaults["max"] = maximum
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    if thresholds is not None:
        defaults["thresholds"] = thresholds
    if custom:
        defaults["custom"] = custom

    return {
        "kind": "Panel",
        "spec": {
            "id": panel_id,
            "title": title,
            "description": description,
            "links": [],
            "data": {
                "kind": "QueryGroup",
                "spec": {
                    "queries": [
                        {
                            "kind": "PanelQuery",
                            "spec": {
                                "query": {
                                    "kind": "DataQuery",
                                    "group": "loki",
                                    "version": "v0",
                                    "datasource": {"name": "$datasource"},
                                    "spec": {
                                        "expr": expr,
                                        "legendFormat": legend,
                                        "queryType": "range",
                                        "editorMode": "code",
                                    },
                                },
                                "refId": chr(ord("A") + index),
                                "hidden": False,
                            },
                        }
                        for index, (expr, legend) in enumerate(queries)
                    ],
                    "transformations": transformations or [],
                    "queryOptions": {},
                },
            },
            "vizConfig": {
                "kind": "VizConfig",
                "group": viz,
                "version": viz_version,
                "spec": {
                    "options": options or {},
                    "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
                },
            },
        },
    }


def logs_panel(panel_id: int, title: str, expr: str, *, description: str = "") -> dict[str, Any]:
    """A logs panel. `queryType` is `range` but there is no legend or unit."""
    return panel(
        panel_id,
        title,
        "logs",
        [(expr, "")],
        description=description,
        options={
            "showTime": True,
            "showLabels": False,
            "showCommonLabels": False,
            "wrapLogMessage": True,
            "prettifyLogMessage": False,
            "enableLogDetails": True,
            "dedupStrategy": "none",
            "sortOrder": "Descending",
        },
    )


_TIMESERIES_CUSTOM = {
    "drawStyle": "line",
    "lineWidth": 1,
    "fillOpacity": 12,
    "showPoints": "never",
    "spanNulls": False,
    "axisBorderShow": False,
    "axisPlacement": "auto",
}
_LEGEND_TABLE = {
    "legend": {
        "displayMode": "table",
        "placement": "bottom",
        "showLegend": True,
        "calcs": ["mean", "max", "lastNotNull"],
    },
    "tooltip": {"mode": "multi", "sort": "desc"},
}
_LEGEND_LIST = {
    "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True, "calcs": []},
    "tooltip": {"mode": "multi", "sort": "desc"},
}
_STAT_OPTIONS = {
    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
    "colorMode": "value",
    "graphMode": "area",
    "justifyMode": "auto",
    "textMode": "auto",
    "wideLayout": True,
}
# Absolute thresholds only. A percentage threshold on a ratio panel reads as a
# percentage of the max, not of 1, which puts the amber band in the wrong place.
_RATIO_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"value": None, "color": "green"},
        {"value": 0.01, "color": "yellow"},
        {"value": 0.05, "color": "red"},
    ],
}
_CACHE_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"value": None, "color": "red"},
        {"value": 0.6, "color": "yellow"},
        {"value": 0.85, "color": "green"},
    ],
}
_TABLE_OPTIONS = {
    "showHeader": True,
    "cellHeight": "sm",
    "footer": {"show": False, "reducer": ["sum"], "countRows": False, "fields": ""},
}
# Turns an instant-vector result into a sorted table. Without the reduce step a
# table panel of a range query shows one column per timestamp.
_TABLE_TRANSFORMS = [
    {"id": "reduce", "options": {"reducers": ["sum"]}},
    {"id": "organize", "options": {"renameByName": {"Field": "", "Total": "Lines"}}},
]


def traffic_dashboard() -> dict[str, Any]:
    """Traffic, cache and latency: the CDN, access and request logs."""
    elements = {
        "panel-1": panel(
            1,
            "Requests/sec",
            "stat",
            [(f"sum(rate({{{BASE}, {HTTP_TYPES}}}[$__rate_interval]))", "requests/sec")],
            description="Every HTTP log line, across the CDN and both access logs.",
            unit="reqps",
            decimals=1,
            options=_STAT_OPTIONS,
        ),
        "panel-2": panel(
            2,
            "Error ratio (5xx)",
            "stat",
            [
                (
                    f"sum(count_over_time({{{BASE}, {HTTP_TYPES}}} "
                    f'| status_class="5xx" [$__range]))'
                    f" / sum(count_over_time({{{BASE}, {HTTP_TYPES}}}[$__range]))",
                    "5xx ratio",
                )
            ],
            description=(
                "`status_class` is structured metadata, so it goes after the `|`. "
                "Putting it inside the `{}` selector returns an empty result with no error."
            ),
            unit="percentunit",
            decimals=2,
            minimum=0,
            thresholds=_RATIO_THRESHOLDS,
            options=_STAT_OPTIONS,
        ),
        "panel-3": panel(
            3,
            "CDN cache hit ratio",
            "stat",
            [
                (
                    f'sum(count_over_time({{{BASE}, log_type="aemcdn"}} | cache_status="HIT" '
                    f'[$__range])) / sum(count_over_time({{{BASE}, log_type="aemcdn"}}[$__range]))',
                    "hit ratio",
                )
            ],
            description=(
                "A low hit ratio is the single biggest driver of AEM publish load. "
                "SYNTH and PASS are counted in the denominator because they are real requests."
            ),
            unit="percentunit",
            decimals=2,
            minimum=0,
            maximum=1,
            thresholds=_CACHE_THRESHOLDS,
            options=_STAT_OPTIONS,
        ),
        "panel-4": panel(
            4,
            "AEM request p99",
            "stat",
            [
                (
                    f'max(quantile_over_time(0.99, {{{BASE}, log_type="aemrequest"}} '
                    f"| unwrap duration_ms [$__range]))",
                    "p99",
                )
            ],
            description=(
                "From the AEM request log's response lines, which only carry a duration "
                "because the shipper pairs them with their request line at write time. "
                "Wrapped in max() deliberately: an unwrap with no aggregation returns one "
                "series per entry."
            ),
            unit="ms",
            decimals=0,
            options=_STAT_OPTIONS,
        ),
        "panel-5": panel(
            5,
            "Request rate by status class",
            "timeseries",
            [
                (
                    f"sum by (status_class) (rate({{{BASE}, {HTTP_TYPES}}}[$__rate_interval]))",
                    "{{status_class}}",
                )
            ],
            description="Aggregating on metadata needs no parser stage.",
            unit="reqps",
            options=_LEGEND_TABLE,
            custom={**_TIMESERIES_CUSTOM, "fillOpacity": 30, "stacking": {"mode": "normal"}},
            overrides=[
                {
                    "matcher": {"id": "byName", "options": klass},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}],
                }
                for klass, col in (
                    ("2xx", "green"),
                    ("3xx", "blue"),
                    ("4xx", "orange"),
                    ("5xx", "red"),
                )
            ],
        ),
        "panel-6": panel(
            6,
            "CDN cache outcome",
            "timeseries",
            [
                (
                    f'sum by (cache_status) (rate({{{BASE}, log_type="aemcdn"}}'
                    f"[$__rate_interval]))",
                    "{{cache_status}}",
                )
            ],
            description="HIT served from the CDN; MISS and PASS reached the publish tier.",
            unit="reqps",
            options=_LEGEND_TABLE,
            custom={**_TIMESERIES_CUSTOM, "fillOpacity": 30, "stacking": {"mode": "normal"}},
        ),
        "panel-7": panel(
            7,
            "CDN latency percentiles (time to first byte)",
            "timeseries",
            [
                (
                    f'quantile_over_time(0.50, {{{BASE}, log_type="aemcdn"}} '
                    f"| unwrap ttfb_ms [$__interval]) by (aem_tier)",
                    "p50 {{aem_tier}}",
                ),
                (
                    f'quantile_over_time(0.95, {{{BASE}, log_type="aemcdn"}} '
                    f"| unwrap ttfb_ms [$__interval]) by (aem_tier)",
                    "p95 {{aem_tier}}",
                ),
                (
                    f'quantile_over_time(0.99, {{{BASE}, log_type="aemcdn"}} '
                    f"| unwrap ttfb_ms [$__interval]) by (aem_tier)",
                    "p99 {{aem_tier}}",
                ),
            ],
            description=(
                "`by (aem_tier)` is not optional. `unwrap` without an aggregation grouping "
                "returns a series per unique metadata combination, which is a series per log line."
            ),
            unit="ms",
            minimum=0,
            options=_LEGEND_TABLE,
            custom=_TIMESERIES_CUSTOM,
        ),
        "panel-8": panel(
            8,
            "AEM request latency percentiles",
            "timeseries",
            [
                (
                    f'quantile_over_time(0.50, {{{BASE}, log_type="aemrequest"}} '
                    f"| unwrap duration_ms [$__interval]) by (aem_tier)",
                    "p50 {{aem_tier}}",
                ),
                (
                    f'quantile_over_time(0.95, {{{BASE}, log_type="aemrequest"}} '
                    f"| unwrap duration_ms [$__interval]) by (aem_tier)",
                    "p95 {{aem_tier}}",
                ),
                (
                    f'quantile_over_time(0.99, {{{BASE}, log_type="aemrequest"}} '
                    f"| unwrap duration_ms [$__interval]) by (aem_tier)",
                    "p99 {{aem_tier}}",
                ),
            ],
            description="Server-side time, as AEM measured it, excluding CDN and network.",
            unit="ms",
            minimum=0,
            options=_LEGEND_TABLE,
            custom=_TIMESERIES_CUSTOM,
        ),
        "panel-9": panel(
            9,
            "Slowest paths (p99)",
            "table",
            [
                (
                    f'topk(15, quantile_over_time(0.99, {{{BASE}, log_type="aemrequest"}} '
                    f"| unwrap duration_ms [$__range]) by (path))",
                    "{{path}}",
                )
            ],
            description=(
                "Latency by path, which is only possible because the request and response "
                "lines were paired on ingest. `path` is metadata, so this costs no streams."
            ),
            unit="ms",
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "Path", "Max": "p99 (ms)"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "p99 (ms)", "desc": True}]},
                },
            ],
        ),
        "panel-10": panel(
            10,
            "Busiest paths",
            "table",
            [
                (
                    f"topk(15, sum by (path) "
                    f"(count_over_time({{{BASE}, {HTTP_TYPES}}}[$__range])))",
                    "{{path}}",
                )
            ],
            description="Where the traffic actually goes.",
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "Path", "Max": "Requests"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "Requests", "desc": True}]},
                },
            ],
        ),
        "panel-11": panel(
            11,
            "Traffic by country",
            "table",
            [
                (
                    f'sum by (country) (count_over_time({{{BASE}, log_type="aemcdn"}}[$__range]))',
                    "{{country}}",
                )
            ],
            description="CDN log only; the AEM access log has no geography.",
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "Country", "Max": "Requests"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "Requests", "desc": True}]},
                },
            ],
        ),
        "panel-12": panel(
            12,
            "Traffic by CDN point of presence",
            "table",
            [
                (
                    f'sum by (pop) (count_over_time({{{BASE}, log_type="aemcdn"}}[$__range]))',
                    "{{pop}}",
                )
            ],
            description=(
                "Where Adobe's CDN served from. A single hot POP usually means one crawler."
            ),
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "POP", "Max": "Requests"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "Requests", "desc": True}]},
                },
            ],
        ),
        "panel-13": logs_panel(
            13,
            "Failed requests (4xx and 5xx)",
            f'{{{BASE}, {HTTP_TYPES}}} | status_class=~"4xx|5xx"',
            description=(
                "Structured metadata is on every line, so open one to see the parsed fields."
            ),
        ),
    }

    layout = [
        ("panel-1", 0, 0, 6, 4),
        ("panel-2", 6, 0, 6, 4),
        ("panel-3", 12, 0, 6, 4),
        ("panel-4", 18, 0, 6, 4),
        ("panel-5", 0, 4, 12, 8),
        ("panel-6", 12, 4, 12, 8),
        ("panel-7", 0, 12, 12, 8),
        ("panel-8", 12, 12, 12, 8),
        ("panel-9", 0, 20, 12, 9),
        ("panel-10", 12, 20, 12, 9),
        ("panel-11", 0, 29, 12, 8),
        ("panel-12", 12, 29, 12, 8),
        ("panel-13", 0, 37, 24, 11),
    ]
    return dashboard(
        uid="adobe-aem-traffic",
        title="Adobe AEM - Traffic and Performance",
        description=(
            "Request rate, status, CDN cache effectiveness and latency for Adobe Experience "
            "Manager as a Cloud Service, from logs shipped by the adobe-aem example. Every "
            "panel aggregates structured metadata with no parser stage."
        ),
        elements=elements,
        layout=layout,
    )


def operations_dashboard() -> dict[str, Any]:
    """Errors, exceptions, per-node health and shipper health."""
    elements = {
        "panel-1": panel(
            1,
            "Log lines/sec by type",
            "timeseries",
            [(f"sum by (log_type) (rate({{{BASE}}}[$__rate_interval]))", "{{log_type}}")],
            description="All seven AEM log types. A type going to zero means forwarding stopped.",
            unit="cps",
            options=_LEGEND_TABLE,
            custom={**_TIMESERIES_CUSTOM, "fillOpacity": 30, "stacking": {"mode": "normal"}},
        ),
        "panel-2": panel(
            2,
            "Errors and warnings/sec",
            "timeseries",
            [
                (
                    f'sum by (level) (rate({{{BASE}, log_type=~"aemerror|aemdispatcher'
                    f'|aemhttpderror", level=~"warn|error|fatal"}}[$__rate_interval]))',
                    "{{level}}",
                )
            ],
            description=(
                "`level` is a real label here, so it belongs inside the `{}` - it is the one "
                "field AEM assigns itself, on the three log types that carry a severity."
            ),
            unit="cps",
            options=_LEGEND_TABLE,
            custom=_TIMESERIES_CUSTOM,
            overrides=[
                {
                    "matcher": {"id": "byName", "options": level},
                    "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}],
                }
                for level, col in (("warn", "orange"), ("error", "red"), ("fatal", "dark-red"))
            ],
        ),
        "panel-3": panel(
            3,
            "ERROR lines",
            "stat",
            [
                (
                    f'sum(count_over_time({{{BASE}, log_type=~"aemerror|aemdispatcher'
                    f'|aemhttpderror", level=~"error|fatal"}}[$__range]))',
                    "errors",
                )
            ],
            unit="short",
            decimals=0,
            thresholds={
                "mode": "absolute",
                "steps": [
                    {"value": None, "color": "green"},
                    {"value": 1, "color": "yellow"},
                    {"value": 100, "color": "red"},
                ],
            },
            options=_STAT_OPTIONS,
        ),
        "panel-4": panel(
            4,
            "Unparsed lines",
            "stat",
            [
                (
                    f'sum(count_over_time({{{BASE}}} | parse_status="unmatched" [$__range]))',
                    "unmatched",
                )
            ],
            description=(
                "Shipper health, not AEM health. Non-zero means a log line arrived in a shape "
                "no parser recognised - it was still shipped, as raw text, so nothing is lost, "
                "but its fields are missing. A sustained non-zero count is worth reporting."
            ),
            unit="short",
            decimals=0,
            thresholds={
                "mode": "absolute",
                "steps": [{"value": None, "color": "green"}, {"value": 1, "color": "orange"}],
            },
            options=_STAT_OPTIONS,
        ),
        "panel-5": panel(
            5,
            "Uncorrelated responses",
            "stat",
            [
                (
                    f'sum(count_over_time({{{BASE}, log_type="aemrequest"}} '
                    f'| correlated="false" [$__range]))',
                    "uncorrelated",
                )
            ],
            description=(
                "Response lines whose request line was in a different object, so they have no "
                "method or path. Expected to be small and non-zero: it is the file-boundary "
                "cost of pairing within one object. A large number means something is wrong "
                "with the request log, not with the shipper."
            ),
            unit="short",
            decimals=0,
            options=_STAT_OPTIONS,
        ),
        "panel-6": panel(
            6,
            "Top loggers by warning and error volume",
            "table",
            [
                (
                    f'topk(15, sum by (logger) (count_over_time({{{BASE}, log_type="aemerror", '
                    f'level=~"warn|error|fatal"}}[$__range])))',
                    "{{logger}}",
                )
            ],
            description=(
                "The Java class doing the complaining. `logger` is metadata rather than a "
                "label on purpose: a busy AEM has hundreds of them, and one stream per logger "
                "would be a cardinality incident."
            ),
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "Logger", "Max": "Lines"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "Lines", "desc": True}]},
                },
            ],
        ),
        "panel-7": panel(
            7,
            "Java exceptions",
            "table",
            [
                (
                    f"topk(15, sum by (exception) (count_over_time({{{BASE}, "
                    f'log_type="aemerror"}} | exception != "" [$__range])))',
                    "{{exception}}",
                )
            ],
            description=(
                "Pulled out of the message text by the parser, including from stack-trace "
                "continuation lines, so a `Caused by:` chain is counted."
            ),
            options=_TABLE_OPTIONS,
            transformations=[
                {"id": "reduce", "options": {"reducers": ["max"]}},
                {
                    "id": "organize",
                    "options": {"renameByName": {"Field": "Exception", "Max": "Count"}},
                },
                {
                    "id": "sortBy",
                    "options": {"fields": {}, "sort": [{"field": "Count", "desc": True}]},
                },
            ],
        ),
        "panel-8": panel(
            8,
            "Log volume by node",
            "timeseries",
            [(f"sum by (node_id) (rate({{{BASE}}}[$__rate_interval]))", "{{node_id}}")],
            description=(
                "One pod much louder or much quieter than its siblings is the earliest signal "
                "of an unhealthy instance. `node_id` is metadata: pods are replaced on every "
                "deploy, so as a label it would leak a stream per pod forever."
            ),
            unit="cps",
            options=_LEGEND_LIST,
            custom=_TIMESERIES_CUSTOM,
        ),
        "panel-9": panel(
            9,
            "Dispatcher cache outcome",
            "timeseries",
            [
                (
                    f'sum by (cache_status) (rate({{{BASE}, log_type="aemdispatcher"}}'
                    f"[$__rate_interval]))",
                    "{{cache_status}}",
                )
            ],
            description=(
                "The dispatcher's own cache, behind the CDN. Empty unless the dispatcher log "
                "is being forwarded - it is a publish-tier log."
            ),
            unit="cps",
            options=_LEGEND_TABLE,
            custom={**_TIMESERIES_CUSTOM, "fillOpacity": 30, "stacking": {"mode": "normal"}},
        ),
        "panel-10": panel(
            10,
            "Ingested bytes by log type",
            "timeseries",
            [(f"sum by (log_type) (bytes_over_time({{{BASE}}}[$__interval]))", "{{log_type}}")],
            description=(
                "What this is costing, split by log type. The usual finding is that aemaccess "
                "and aemrequest dominate and that health-check traffic is most of it - filter "
                "those at the forwarding config if so, not here."
            ),
            unit="bytes",
            options=_LEGEND_TABLE,
            custom={**_TIMESERIES_CUSTOM, "fillOpacity": 30, "stacking": {"mode": "normal"}},
        ),
        "panel-11": logs_panel(
            11,
            "Errors and warnings",
            f'{{{BASE}, log_type=~"aemerror|aemdispatcher|aemhttpderror", '
            f'level=~"warn|error|fatal"}}',
            description="Expand a line to see logger, thread, node and exception as fields.",
        ),
    }

    layout = [
        ("panel-3", 0, 0, 6, 4),
        ("panel-4", 6, 0, 6, 4),
        ("panel-5", 12, 0, 6, 4),
        ("panel-1", 18, 0, 6, 4),
        ("panel-2", 0, 4, 12, 8),
        ("panel-8", 12, 4, 12, 8),
        ("panel-6", 0, 12, 12, 9),
        ("panel-7", 12, 12, 12, 9),
        ("panel-9", 0, 21, 12, 8),
        ("panel-10", 12, 21, 12, 8),
        ("panel-11", 0, 29, 24, 11),
    ]
    return dashboard(
        uid="adobe-aem-operations",
        title="Adobe AEM - Errors and Operations",
        description=(
            "Errors, exceptions, per-node health and shipper health for Adobe Experience "
            "Manager as a Cloud Service, from logs shipped by the adobe-aem example. The "
            "unparsed-lines and uncorrelated-responses panels watch the shipper itself."
        ),
        elements=elements,
        layout=layout,
    )


def variables() -> list[dict[str, Any]]:
    """Datasource plus the three label dimensions worth slicing by.

    Only labels can be dashboard variables driven by `label_values`, which is
    another consequence of the label contract: there is no `$path` variable
    because `path` is metadata. Filter on it in the query with `| path=~"..."`.
    """
    return [
        {
            "kind": "DatasourceVariable",
            "spec": {
                "name": "datasource",
                "pluginId": "loki",
                "refresh": "onDashboardLoad",
                "current": {"text": "", "value": ""},
                "label": "Loki data source",
                "hide": "dontHide",
                "skipUrlSync": False,
                "multi": False,
                "options": [],
                "allowCustomValue": False,
            },
        },
        {
            "kind": "QueryVariable",
            "spec": {
                "name": "service",
                "current": {"text": "adobe-aem", "value": "adobe-aem"},
                "label": "Service",
                "hide": "dontHide",
                "refresh": "onDashboardLoad",
                "skipUrlSync": False,
                "query": {
                    "kind": "DataQuery",
                    "group": "loki",
                    "version": "v0",
                    "datasource": {"name": "$datasource"},
                    "spec": {
                        "label": "service_name",
                        "refId": "LokiVariableQueryEditor-VariableQuery",
                        "type": 1,
                    },
                },
                "definition": "label_values(service_name)",
                "regex": "",
                "sort": "alphabeticalAsc",
                "options": [],
                "multi": False,
                "includeAll": False,
                "allowCustomValue": True,
            },
        },
        {
            "kind": "QueryVariable",
            "spec": {
                "name": "env_id",
                "current": {"text": "All", "value": "$__all"},
                "label": "AEM environment",
                "hide": "dontHide",
                "refresh": "onTimeRangeChanged",
                "skipUrlSync": False,
                "query": {
                    "kind": "DataQuery",
                    "group": "loki",
                    "version": "v0",
                    "datasource": {"name": "$datasource"},
                    "spec": {
                        "label": "aem_env_id",
                        "stream": '{service_name=~"$service"}',
                        "refId": "LokiVariableQueryEditor-VariableQuery",
                        "type": 1,
                    },
                },
                "definition": "label_values(aem_env_id)",
                "regex": "",
                "sort": "alphabeticalAsc",
                "options": [],
                "multi": True,
                "includeAll": True,
                # `.+` rather than `.*`, so "All" still requires the label to be
                # present. With `.*` a stream missing the label is also matched,
                # which silently mixes environments that set it with ones that
                # do not.
                "allValue": ".+",
                "allowCustomValue": True,
            },
        },
        {
            "kind": "QueryVariable",
            "spec": {
                "name": "tier",
                "current": {"text": "All", "value": "$__all"},
                "label": "AEM tier",
                "hide": "dontHide",
                "refresh": "onTimeRangeChanged",
                "skipUrlSync": False,
                "query": {
                    "kind": "DataQuery",
                    "group": "loki",
                    "version": "v0",
                    "datasource": {"name": "$datasource"},
                    "spec": {
                        "label": "aem_tier",
                        "stream": '{service_name=~"$service"}',
                        "refId": "LokiVariableQueryEditor-VariableQuery",
                        "type": 1,
                    },
                },
                "definition": "label_values(aem_tier)",
                "regex": "",
                "sort": "alphabeticalAsc",
                "options": [],
                "multi": True,
                "includeAll": True,
                "allValue": ".+",
                "allowCustomValue": True,
            },
        },
    ]


def dashboard(
    *,
    uid: str,
    title: str,
    description: str,
    elements: dict[str, Any],
    layout: list[tuple[str, int, int, int, int]],
) -> dict[str, Any]:
    return {
        "apiVersion": "dashboard.grafana.app/v2",
        "kind": "Dashboard",
        "metadata": {"name": uid},
        "spec": {
            "title": title,
            "description": description,
            "tags": ["adobe-aem", "loki", "grafana-cloud-reference-examples"],
            "cursorSync": "Crosshair",
            "editable": True,
            "liveNow": False,
            "preload": False,
            "links": [],
            "annotations": [],
            "timeSettings": {
                "timezone": "browser",
                "from": "now-6h",
                "to": "now",
                "autoRefresh": "",
                "autoRefreshIntervals": ["10s", "30s", "1m", "5m", "15m", "30m", "1h"],
                "hideTimepicker": False,
                "fiscalYearStartMonth": 0,
            },
            "variables": variables(),
            "elements": elements,
            "layout": {
                "kind": "GridLayout",
                "spec": {
                    "items": [
                        {
                            "kind": "GridLayoutItem",
                            "spec": {
                                "x": x,
                                "y": y,
                                "width": width,
                                "height": height,
                                "element": {"kind": "ElementReference", "name": name},
                            },
                        }
                        for name, x, y, width, height in layout
                    ]
                },
            },
        },
    }


DASHBOARDS = {
    "traffic-and-performance.json": traffic_dashboard,
    "errors-and-operations.json": operations_dashboard,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed JSON differs from what this generates",
    )
    args = parser.parse_args(argv)

    DASHBOARDS_DIR.mkdir(parents=True, exist_ok=True)
    drifted: list[str] = []
    for name, build in DASHBOARDS.items():
        rendered = json.dumps(build(), indent=2, sort_keys=False) + "\n"
        target = DASHBOARDS_DIR / name
        if args.check:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != rendered:
                drifted.append(name)
        else:
            target.write_text(rendered, encoding="utf-8")
            print(f"wrote {target.relative_to(DASHBOARDS_DIR.parent)}")

    if drifted:
        print(
            "dashboards/ is out of date with dev/build_dashboards.py: "
            + ", ".join(drifted)
            + "\nRun `just aem-dashboards`.",
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
