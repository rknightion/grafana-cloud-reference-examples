from __future__ import annotations

import json

from docdb_connection_attribution.audit import OVERLAP_MS, ingest, parse_event
from docdb_connection_attribution.store import AttributionStore
from docdb_fakes import FakeDynamoDB, FakeLogs, auth_event

NOW = 1_800_000_000_000


def _store(ddb: FakeDynamoDB) -> AttributionStore:
    return AttributionStore(ddb, "t", ttl_days=14)


def test_parse_event_keeps_successful_authentications_only() -> None:
    ok = json.loads(auth_event("i1", "192.0.2.10:5000", "app", 1)["message"])
    assert parse_event(json.dumps(ok)) == ("192.0.2.10:5000", "app", 1)
    failed = json.loads(auth_event("i1", "192.0.2.10:5000", "app", 1, success=False)["message"])
    assert parse_event(json.dumps(failed)) is None
    assert parse_event(json.dumps({"atype": "authCheck", "ts": 1})) is None
    assert parse_event("not json") is None


def test_latest_event_wins_per_instance_and_socket(ddb: FakeDynamoDB) -> None:
    logs = FakeLogs(
        [
            [
                # Out of order on purpose: FilterLogEvents does not promise time order.
                auth_event("i1", "192.0.2.10:5000", "new", 2),
                auth_event("i1", "192.0.2.10:5000", "old", 1),
                auth_event("i2", "192.0.2.10:5000", "other-instance", 3),
            ]
        ]
    )
    store = _store(ddb)
    ingest(logs, store, "g", backfill_minutes=60, time_left_ms=lambda: 60_000, now_ms=lambda: NOW)
    found = store.lookup([("i1", "192.0.2.10:5000"), ("i2", "192.0.2.10:5000")])
    assert found == {("i1", "192.0.2.10:5000"): "new", ("i2", "192.0.2.10:5000"): "other-instance"}


def test_first_run_backfills_then_overlaps_the_next_window(ddb: FakeDynamoDB) -> None:
    logs = FakeLogs([[]])
    store = _store(ddb)
    ingest(logs, store, "g", backfill_minutes=60, time_left_ms=lambda: 60_000, now_ms=lambda: NOW)
    assert logs.calls[0]["startTime"] == NOW - 60 * 60_000
    assert store.load_checkpoint().next_start_ms == NOW - OVERLAP_MS


def test_an_interrupted_read_resumes_the_same_window(ddb: FakeDynamoDB) -> None:
    pages = [[auth_event("i1", f"192.0.2.10:{p}", f"u{p}", p)] for p in range(3)]
    store = _store(ddb)

    first = ingest(
        FakeLogs(pages),
        store,
        "g",
        backfill_minutes=60,
        time_left_ms=lambda: 1_000,
        now_ms=lambda: NOW,
    )
    assert not first.complete
    saved = store.load_checkpoint()
    assert saved.resume_token == "1"

    logs = FakeLogs(pages)
    second = ingest(
        logs,
        store,
        "g",
        backfill_minutes=60,
        time_left_ms=lambda: 60_000,
        now_ms=lambda: NOW + 60_000,
    )
    assert second.complete
    assert logs.calls[0]["nextToken"] == "1"
    assert logs.calls[0]["startTime"] == saved.resume_start_ms
    assert logs.calls[0]["endTime"] == saved.resume_end_ms
    assert store.lookup([("i1", "192.0.2.10:2")]) == {("i1", "192.0.2.10:2"): "u2"}
    assert store.load_checkpoint().resume_token is None
