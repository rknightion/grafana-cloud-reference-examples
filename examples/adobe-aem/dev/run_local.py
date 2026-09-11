#!/usr/bin/env python3
"""Push AEM log data to Grafana Cloud Loki from a laptop, with no AWS involved.

Two modes, both running the **real** parsers, the real labelling and the real
Loki client, so what lands in Loki is what the Lambda would have produced:

  fixtures   Parse the committed fixtures and push them once. The fastest way to
             confirm a dashboard query works against the exact shape of data the
             function produces.
  synth      Generate AEM-shaped traffic continuously. What you want when
             building or demoing a dashboard, because the fixtures are one
             historical day and a dashboard on `now-1h` shows nothing.

## Timestamps are rewritten, and they have to be

Grafana Cloud Loki rejects a sample older than `reject_old_samples_max_age`,
one week by default, with a 400 that no retry fixes and that takes the whole
batch with it. The fixtures are a single historical day. So both modes shift
every timestamp forward by a fixed offset, preserving the *spacing* between
lines - the shape of a traffic spike survives, only its position moves. Without
this the fixtures mode fails outright on any tenant with the default limit.

## Credentials come from the environment only

Nothing in this file knows an endpoint, a tenant id or a token. It reads the
same four variables the deployed function does, so a working local push proves
the deployed configuration shape too:

    GRAFANA_CLOUD_LOKI_ENDPOINT     e.g. https://logs-prod-012.grafana.net
    GRAFANA_CLOUD_LOKI_TENANT_ID    the numeric Loki tenant id, not the stack id
    GRAFANA_CLOUD_LOKI_TOKEN        a Cloud Access Policy token with logs:write
    AEM_PROGRAM_ID / AEM_ENV_ID / AEM_ENV_TYPE      optional stream labels

`GRAFANA_CLOUD_LOKI_TOKEN` is for local use only. The shipped IaC never sets it;
it uses `GRAFANA_CLOUD_CREDENTIALS_SECRET_ID` and Secrets Manager.
"""

from __future__ import annotations

import argparse
import collections
import os
import random
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

# The example's own package and the shared library are both workspace members, so
# `uv run` resolves them. This runs from the repository, never from the zip.
from adobe_aem import LogType, RequestCorrelator, parser_for
from adobe_aem.classify import (
    DEFAULT_KEY_PATTERN,
    compile_key_pattern,
    from_key,
    tier_from_name,
)
from adobe_aem.labels import STATUS_CLASS_LEVELS, build_labels, detected_level_for
from adobe_aem.logtypes import Tier
from adobe_aem.parsers import ParsedRecord
from grafana_cloud_common import (
    LogEntry,
    LokiClient,
    LokiConfig,
    StreamBatcher,
    configure,
    get_logger,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

configure()
_LOG = get_logger("adobe_aem.dev")


def _client() -> tuple[LokiClient, LokiConfig]:
    """Build the same Loki client the Lambda uses, with a static local token.

    `LokiClient` takes a token *provider*, not a token, so the deployed function
    can re-read a rotated secret without being rebuilt. Locally there is nothing
    to rotate, so the provider is a constant.
    """
    config = LokiConfig.from_env()
    if not config.token:
        raise SystemExit(
            "Set GRAFANA_CLOUD_LOKI_TOKEN for local pushes. "
            "GRAFANA_CLOUD_CREDENTIALS_SECRET_ID is the deployed path and needs AWS."
        )
    token = config.token
    return LokiClient(config, lambda: token), config


def _push(
    client: LokiClient, config: LokiConfig, pairs: Iterator[tuple[dict[str, str], LogEntry]]
) -> int:
    batcher = StreamBatcher(max_lines=config.batch_max_lines, max_bytes=config.batch_max_bytes)
    shipped = 0
    for batch in batcher.drain(pairs):
        shipped += client.push(batch)
    return shipped


# --- fixtures mode -----------------------------------------------------------


def fixture_entries(
    *, target_ns: int, only: str | None, service_name: str, defaults: dict[str, str]
) -> Iterator[tuple[dict[str, str], LogEntry]]:
    """Parse every fixture, landing each file's newest line at `target_ns`.

    **The shift is computed per file, not once across all of them.** One global
    offset looks right and is not: the four real fixtures each span a whole log
    day, while the three hand-written ones span about a minute at the start of
    that day. Shifting everything by the offset that puts the global newest line
    an hour ago therefore dropped the three short files roughly 24 hours into the
    past, where a dashboard on `now-6h` cannot see them at all - and the symptom
    is three log types silently missing from every panel.

    Per-file shifting makes the files overlap in recent time, which is also what
    they are: concurrent logs from one system.
    """
    pattern = compile_key_pattern(DEFAULT_KEY_PATTERN)
    for path in sorted(FIXTURES.glob("*.log")):
        coordinates = from_key(path.name, pattern)
        log_type = (
            LogType(coordinates["log_type"]) if "log_type" in coordinates else LogType.UNKNOWN
        )
        if only and str(log_type) != only:
            continue
        tier = tier_from_name(coordinates.get("tier", ""))
        parse = parser_for(log_type)
        correlator = RequestCorrelator() if log_type is LogType.AEM_REQUEST else None

        # Buffered rather than streamed, because the shift cannot be known until
        # the newest line in this file has been seen. 250 lines per fixture, so
        # the memory is irrelevant; the Lambda path streams and is unaffected.
        parsed: list[tuple[int, ParsedRecord]] = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            # The line number is attached to the record before the correlator
            # sees it, exactly as the handler does. The correlator releases a
            # buffered record later, so a number taken from the loop at emit
            # time belongs to the wrong line - flushed entries were getting 0.
            numbered = parse(line)
            numbered = replace(numbered, metadata={**numbered.metadata, "record": str(number)})
            records = correlator.process(numbered) if correlator else [numbered]
            parsed.extend((number, record) for record in records)
        if correlator:
            parsed.extend(
                (int(record.metadata.get("record", "0")), record) for record in correlator.flush()
            )

        newest = max(
            (r.timestamp_ns for _, r in parsed if r.timestamp_ns is not None), default=None
        )
        shift_ns = target_ns - newest if newest is not None else 0
        for number, record in parsed:
            yield _entry(
                record,
                log_type,
                tier,
                coordinates,
                service_name,
                path.name,
                number,
                shift_ns,
                defaults,
            )
        _LOG.info(
            "fixture read",
            fixture=path.name,
            log_type=str(log_type),
            entries=len(parsed),
            shift_hours=round(shift_ns / 3.6e12, 1),
        )


def _entry(
    record: ParsedRecord,
    log_type: LogType,
    tier: Tier,
    coordinates: dict[str, str],
    service_name: str,
    source: str,
    number: int,
    shift_ns: int,
    defaults: dict[str, str],
) -> tuple[dict[str, str], LogEntry]:
    labels = build_labels(
        service_name=service_name,
        log_type=log_type,
        tier=tier,
        coordinates=coordinates,
        level=record.level,
        # The same AEM_* environment variables the deployed function reads. Not
        # passing them produced fixture streams with no `aem_env_id` label at
        # all, which a dashboard variable defaulting to `.+` correctly excluded -
        # so three log types were silently missing from every panel.
        default_program_id=defaults.get("aem_program_id", ""),
        default_env_id=defaults.get("aem_env_id", ""),
        default_env_type=defaults.get("aem_env_type", ""),
    )
    metadata = {**record.metadata, "object_key": f"dev/{source}", "record": str(number)}
    # Mirrors the handler exactly: a log type with no AEM severity gets an
    # inferred `detected_level` in metadata. If this drifts from the handler the
    # local data stops matching what the function produces, which defeats the
    # point of testing a dashboard against it.
    if "level" not in labels:
        inferred = record.level or detected_level_for(metadata)
        if inferred:
            metadata["detected_level"] = inferred
    return labels, LogEntry(
        timestamp_ns=(
            record.timestamp_ns + shift_ns if record.timestamp_ns is not None else time.time_ns()
        ),
        line=record.message,
        structured_metadata=metadata,
    )


# --- synth mode --------------------------------------------------------------
#
# Deliberately not a copy of the fixtures on a loop: a dashboard built against
# eleven repeating paths and two status codes looks finished and then falls over
# on real data. These distributions are skewed the way a real AEM publish tier
# is - a long tail of paths, cache hits dominating, a small constant trickle of
# 404s from crawlers, and occasional slow requests that make a p99 panel mean
# something.

_PATHS = (
    ("/content/examplecorp/en/home.html", 30),
    ("/content/examplecorp/en/products.html", 18),
    ("/content/examplecorp/en/products/widget.html", 12),
    ("/content/examplecorp/en/search.html", 8),
    ("/content/examplecorp/en/contact.html", 5),
    ("/content/examplecorp/en/about.html", 4),
    ("/etc.clientlibs/examplecorp/clientlibs/clientlib-base.css", 20),
    ("/etc.clientlibs/examplecorp/clientlibs/clientlib-base.js", 18),
    ("/content/dam/examplecorp/images/hero.jpg", 14),
    ("/content/dam/examplecorp/documents/brochure.pdf", 3),
    ("/libs/granite/csrf/token.json", 6),
    ("/bin/querybuilder.json", 1),
    ("/content/examplecorp/en/missing.html", 2),
)
_COUNTRIES = (("GB", 34), ("US", 28), ("DE", 12), ("FR", 9), ("IN", 7), ("CA", 6), ("AU", 4))
_POPS = (("LHR", 30), ("IAD", 22), ("FRA", 14), ("CDG", 10), ("BOM", 8), ("YYZ", 8), ("SYD", 8))
_AGENTS = (
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.5 Safari/605.1.15",
        34,
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        40,
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
        18,
    ),
    ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", 5),
    ("python-requests/2.32.3", 3),
)
_LOGGERS = (
    ("com.adobe.cq.assetcompute.impl.scanprocess.AssetMonitorMBeanImpl", 20),
    ("org.apache.jackrabbit.oak.plugins.document.JournalGarbageCollector", 16),
    ("com.day.cq.rewriter.linkchecker.impl.LinkInfoStorageImpl", 12),
    ("com.adobe.aem.repoapi.impl.MvcRequestHandler", 10),
    ("org.apache.sling.event.impl.jobs.queues.JobQueueImpl", 8),
    ("com.day.cq.replication.impl.ReplicatorImpl", 6),
    ("org.apache.sling.commons.scheduler.impl.QuartzScheduler", 5),
)
_EXCEPTIONS = (
    "javax.jcr.RepositoryException",
    "org.apache.sling.api.SlingException",
    "java.util.concurrent.TimeoutException",
    "com.day.cq.replication.ReplicationException",
)
_NODES = (
    "cm-p12345-e67890-aem-publish-fr8h4lctr-qs632",
    "cm-p12345-e67890-aem-publish-fr8h4lctr-6liqc",
    "cm-p12345-e67890-aem-publish-fr8h4lctr-w4k81",
)


def _weighted(rng: random.Random, options: tuple[tuple[str, int], ...]) -> str:
    total = sum(weight for _, weight in options)
    target = rng.randrange(total)
    for value, weight in options:
        target -= weight
        if target < 0:
            return value
    return options[-1][0]


def _status(rng: random.Random, path: str) -> tuple[str, str]:
    if path.endswith("missing.html"):
        return "404", "4xx"
    if path == "/bin/querybuilder.json":
        return "403", "4xx"
    roll = rng.random()
    if roll < 0.004:
        return "500", "5xx"
    if roll < 0.012:
        return "503", "5xx"
    if roll < 0.08:
        return "304", "3xx"
    return "200", "2xx"


def _latency_ms(rng: random.Random, cache_status: str, status: str) -> int:
    """A long-tailed latency, so a p99 panel shows something a p50 panel does not."""
    if status.startswith("5"):
        return rng.randint(3000, 8000)
    if cache_status == "HIT":
        return max(1, int(rng.lognormvariate(1.4, 0.5)))
    return max(4, int(rng.lognormvariate(4.2, 0.8)))


def synth_entries(
    rng: random.Random, *, count: int, base_labels: dict[str, str]
) -> Iterator[tuple[dict[str, str], LogEntry]]:
    """Generate `count` entries across the CDN, access, request and error logs."""
    now = time.time_ns()
    for index in range(count):
        # Spread over the last few seconds so a 'now' dashboard has a curve
        # rather than a single spike.
        timestamp = now - rng.randrange(0, 5_000_000_000)
        node = rng.choice(_NODES)
        roll = rng.random()
        if roll < 0.45:
            yield _synth_cdn(rng, timestamp, base_labels)
        elif roll < 0.70:
            yield _synth_access(rng, timestamp, node, base_labels)
        elif roll < 0.90:
            yield _synth_request(rng, timestamp, node, base_labels, index)
        else:
            yield _synth_error(rng, timestamp, node, base_labels)


def _labels(base: dict[str, str], log_type: LogType, level: str | None = None) -> dict[str, str]:
    labels = {**base, "log_type": str(log_type), "aem_tier": str(Tier.PUBLISH)}
    if level:
        labels["level"] = level
    return labels


def _synth_cdn(
    rng: random.Random, timestamp: int, base: dict[str, str]
) -> tuple[dict[str, str], LogEntry]:
    path = _weighted(rng, _PATHS)
    cache_status = _weighted(rng, (("HIT", 62), ("MISS", 22), ("PASS", 12), ("SYNTH", 4)))
    status, klass = _status(rng, path)
    ttfb = _latency_ms(rng, cache_status, status)
    metadata = {
        "client_ip": f"203.0.113.{rng.randrange(1, 255)}",
        "country": _weighted(rng, _COUNTRIES),
        "pop": _weighted(rng, _POPS),
        "user_agent": _weighted(rng, _AGENTS),
        "host": "publish-p12345-e67890.adobeaemcloud.com",
        "path": path,
        "method": "GET",
        "cache_status": cache_status,
        "status": status,
        "status_class": klass,
        "detected_level": STATUS_CLASS_LEVELS[klass],
        "ttfb_ms": str(ttfb),
        "ttlb_ms": str(ttfb + rng.randrange(0, 12)),
        "aem_tenant": "examplecorp",
        "aem_env_kind": "SKYLINE",
        "ddos": "false",
        "object_key": "dev/synth",
    }
    return _labels(base, LogType.AEM_CDN), LogEntry(
        timestamp_ns=timestamp,
        line=f'{{"url":"{path}","status":{status},"cache":"{cache_status}","ttfb":{ttfb}}}',
        structured_metadata=metadata,
    )


def _synth_access(
    rng: random.Random, timestamp: int, node: str, base: dict[str, str]
) -> tuple[dict[str, str], LogEntry]:
    path = _weighted(rng, _PATHS)
    status, klass = _status(rng, path)
    metadata = {
        "node_id": node,
        "client_ip": f"203.0.113.{rng.randrange(1, 255)}",
        "method": "GET",
        "path": path,
        "protocol": "HTTP/1.1",
        "status": status,
        "status_class": klass,
        "bytes_sent": str(rng.randrange(200, 300_000)),
        "detected_level": STATUS_CLASS_LEVELS[klass],
        "user_agent": _weighted(rng, _AGENTS),
        "object_key": "dev/synth",
    }
    return _labels(base, LogType.AEM_ACCESS), LogEntry(
        timestamp_ns=timestamp,
        line=f"GET {path} HTTP/1.1",
        structured_metadata=metadata,
    )


def _synth_request(
    rng: random.Random, timestamp: int, node: str, base: dict[str, str], index: int
) -> tuple[dict[str, str], LogEntry]:
    path = _weighted(rng, _PATHS)
    status, klass = _status(rng, path)
    duration = _latency_ms(rng, "MISS", status)
    metadata = {
        "node_id": node,
        "request_id": str(10_000 + index),
        "direction": "response",
        "method": "GET",
        "path": path,
        "protocol": "HTTP/1.1",
        "status": status,
        "status_class": klass,
        "content_type": "text/html",
        "duration_ms": str(duration),
        "detected_level": STATUS_CLASS_LEVELS[klass],
        "correlated": "true",
        "object_key": "dev/synth",
    }
    return _labels(base, LogType.AEM_REQUEST), LogEntry(
        timestamp_ns=timestamp,
        line=f"{status} text/html {duration}ms",
        structured_metadata=metadata,
    )


def _synth_error(
    rng: random.Random, timestamp: int, node: str, base: dict[str, str]
) -> tuple[dict[str, str], LogEntry]:
    level = _weighted(rng, (("info", 70), ("warn", 22), ("error", 8)))
    logger = _weighted(rng, _LOGGERS)
    metadata = {
        "node_id": node,
        "logger": logger,
        "thread": f"sling-default-{rng.randrange(1, 12)}-{logger}",
        "object_key": "dev/synth",
    }
    if level == "error":
        exception = rng.choice(_EXCEPTIONS)
        metadata["exception"] = exception
        message = f"{logger} failed to process: {exception}: connection reset"
    elif level == "warn":
        message = f"{logger} retrying after transient failure"
    else:
        message = f"{logger} completed run in {rng.randrange(2, 900)}ms"
    return _labels(base, LogType.AEM_ERROR, level), LogEntry(
        timestamp_ns=timestamp, line=message, structured_metadata=metadata
    )


# --- entry point -------------------------------------------------------------


def _base_labels(service_name: str) -> dict[str, str]:
    labels = {"service_name": service_name}
    for variable, label in (
        ("AEM_PROGRAM_ID", "aem_program_id"),
        ("AEM_ENV_ID", "aem_env_id"),
        ("AEM_ENV_TYPE", "aem_env_type"),
    ):
        value = (os.environ.get(variable) or "").strip()
        if value:
            labels[label] = value
    return labels


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("fixtures", "synth"), help="push the committed fixtures, or generate"
    )
    parser.add_argument(
        "--service-name",
        default="adobe-aem-dev",
        help="service_name label, kept distinct from a real deployment (default: %(default)s)",
    )
    parser.add_argument("--log-type", default=None, help="fixtures mode: push only this log type")
    parser.add_argument(
        "--shift-hours",
        type=float,
        default=1.0,
        help="fixtures mode: land the newest fixture line this many hours ago "
        "(default: %(default)s). Loki rejects samples older than the tenant's "
        "reject_old_samples_max_age, so this is not optional for a historical fixture.",
    )
    parser.add_argument("--count", type=int, default=500, help="synth mode: entries per round")
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="synth mode: rounds to push, 0 for forever (default: %(default)s)",
    )
    parser.add_argument(
        "--interval", type=float, default=15.0, help="synth mode: seconds between rounds"
    )
    parser.add_argument("--seed", type=int, default=None, help="synth mode: RNG seed")
    parser.add_argument("--dry-run", action="store_true", help="print a summary instead of pushing")
    args = parser.parse_args(argv)

    base_labels = _base_labels(args.service_name)

    if args.mode == "fixtures":
        target = time.time_ns() - int(args.shift_hours * 3_600 * 1_000_000_000)
        pairs = fixture_entries(
            target_ns=target,
            only=args.log_type,
            service_name=args.service_name,
            defaults=base_labels,
        )
        if args.dry_run:
            return _summarise(pairs)
        client, config = _client()
        shipped = _push(client, config, pairs)
        _LOG.info("fixtures pushed", entries=shipped, newest_line_hours_ago=args.shift_hours)
        return 0

    rng = random.Random(args.seed)
    if args.dry_run:
        return _summarise(synth_entries(rng, count=args.count, base_labels=base_labels))

    client, config = _client()
    round_number = 0
    while args.rounds == 0 or round_number < args.rounds:
        round_number += 1
        shipped = _push(
            client, config, synth_entries(rng, count=args.count, base_labels=base_labels)
        )
        _LOG.info("synth round pushed", round=round_number, entries=shipped)
        if args.rounds != 0 and round_number >= args.rounds:
            break
        time.sleep(args.interval)
    return 0


def _summarise(pairs: Iterator[tuple[dict[str, str], LogEntry]]) -> int:
    streams: collections.Counter[str] = collections.Counter()
    fields: collections.Counter[str] = collections.Counter()
    total = 0
    for labels, entry in pairs:
        total += 1
        streams[",".join(f"{k}={v}" for k, v in sorted(labels.items()))] += 1
        fields.update(entry.structured_metadata.keys())
    print(f"{total} entries across {len(streams)} streams")
    for stream, count in streams.most_common():
        print(f"  {count:6}  {stream}")
    print(f"\nstructured metadata fields: {', '.join(sorted(fields))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
