from __future__ import annotations

import time

from docdb_connection_attribution.store import AttributionStore, mapping_key
from docdb_fakes import FakeDynamoDB


def test_an_older_event_never_overwrites_a_newer_user(ddb: FakeDynamoDB) -> None:
    store = AttributionStore(ddb, "t", ttl_days=14)
    store.record("i1", "192.0.2.10:5000", "new", 20)
    store.record("i1", "192.0.2.10:5000", "new", 20)  # the overlap re-reads the same event
    store.record("i1", "192.0.2.10:5000", "old", 10)  # delivered late, out of order
    assert store.lookup([("i1", "192.0.2.10:5000")]) == {("i1", "192.0.2.10:5000"): "new"}


def test_lookup_batches_and_retries_unprocessed_keys() -> None:
    ddb = FakeDynamoDB(unprocessed_first=5)
    store = AttributionStore(ddb, "t", ttl_days=14)
    keys = [("i1", f"192.0.2.10:{p}") for p in range(250)]
    for i, c in keys:
        store.record(i, c, "u", 1)
    assert len(store.lookup(keys)) == 250
    assert ddb.batch_calls == 4  # three chunks of up to 100, plus one retry of held-back keys


def test_a_hit_near_expiry_gets_its_ttl_extended(ddb: FakeDynamoDB) -> None:
    store = AttributionStore(ddb, "t", ttl_days=14)
    store.record("i1", "192.0.2.10:5000", "u", 1)
    store.record("i1", "192.0.2.10:6000", "u", 1)
    pk = mapping_key("i1", "192.0.2.10:5000")
    ddb.items[pk]["expires_at"] = {"N": str(int(time.time()) + 3600)}
    store.lookup([("i1", "192.0.2.10:5000"), ("i1", "192.0.2.10:6000")])
    assert ddb.updates == [pk]
