"""Read DocumentDB audit `authenticate` events into the attribution store.

Each authenticate event records the client socket (``remote_ip``, which includes the port) and
the user in ``param.user``; the top-level ``user`` field is empty on these events. The audit log
has one stream per instance, named after the instance identifier, so the stream name scopes the
socket to its instance.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from grafana_cloud_common import get_logger

from .store import AttributionStore, Checkpoint

_LOG = get_logger(__name__)

AUTHENTICATE_PATTERN = '{ $.atype = "authenticate" }'
# CloudWatch Logs can deliver an event a minute or two after its timestamp. Each window starts
# this far before the previous one ended, and the store's condition makes the re-read harmless.
OVERLAP_MS = 5 * 60 * 1000


class LogsSender(Protocol):
    def filter_log_events(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class IngestResult:
    events: int
    mappings: int
    complete: bool


def parse_event(message: str) -> tuple[str, str, int] | None:
    """(remote_ip, user, ts_ms) for a successful authenticate event, else None."""
    try:
        m = json.loads(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(m, dict) or m.get("atype") != "authenticate":
        return None
    param = m.get("param")
    if not isinstance(param, dict) or param.get("success") is not True:
        return None
    user, remote, ts = param.get("user"), m.get("remote_ip"), m.get("ts")
    if not user or not remote or not isinstance(ts, int):
        return None
    return str(remote), str(user), ts


def ingest(
    logs: LogsSender,
    store: AttributionStore,
    log_group: str,
    *,
    backfill_minutes: int,
    time_left_ms: Callable[[], int],
    reserve_ms: int = 20_000,
    now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> IngestResult:
    """Read new authenticate events, stopping early to leave ``reserve_ms`` for the export.

    A run that stops early saves its page token, and the next run finishes the same window
    before starting a new one, so a first-run backfill of a busy cluster spreads over several
    runs instead of timing out.
    """
    checkpoint = store.load_checkpoint()
    if checkpoint.resume_token and checkpoint.resume_start_ms and checkpoint.resume_end_ms:
        start, end, token = (
            checkpoint.resume_start_ms,
            checkpoint.resume_end_ms,
            checkpoint.resume_token,
        )
    else:
        end = now_ms()
        start = checkpoint.next_start_ms or end - backfill_minutes * 60_000
        token = None

    latest: dict[tuple[str, str], tuple[int, str]] = {}
    events = 0
    while True:
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "startTime": start,
            "endTime": end,
            "filterPattern": AUTHENTICATE_PATTERN,
        }
        if token:
            kwargs["nextToken"] = token
        page = logs.filter_log_events(**kwargs)
        for event in page.get("events", []):
            parsed = parse_event(event.get("message", ""))
            if parsed is None:
                continue
            events += 1
            remote, user, ts = parsed
            key = (str(event.get("logStreamName", "")), remote)
            if key not in latest or ts > latest[key][0]:
                latest[key] = (ts, user)
        token = page.get("nextToken")
        if not token or time_left_ms() < reserve_ms:
            break

    # Dedupe within the run first, so a busy socket costs one write rather than one per event.
    for (instance, remote), (ts, user) in latest.items():
        store.record(instance, remote, user, ts)

    complete = token is None
    if complete:
        store.save_checkpoint(Checkpoint(next_start_ms=max(start, end - OVERLAP_MS)))
    else:
        store.save_checkpoint(
            Checkpoint(
                next_start_ms=checkpoint.next_start_ms,
                resume_start_ms=start,
                resume_end_ms=end,
                resume_token=token,
            )
        )
        _LOG.warning("audit read incomplete, resuming next run", window_start_ms=start)
    return IngestResult(events=events, mappings=len(latest), complete=complete)
