from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from grafana_cloud_common.batching import StreamBatcher
from grafana_cloud_common.loki import LogEntry

LABELS = {"service_name": "test"}


def pairs(count: int, *, size: int = 10) -> Iterator[tuple[Mapping[str, str], LogEntry]]:
    for index in range(count):
        yield LABELS, LogEntry(index, "x" * size)


class TestStreamBatcher:
    def test_rejects_non_positive_bounds(self) -> None:
        with pytest.raises(ValueError, match="must both be positive"):
            StreamBatcher(max_lines=0, max_bytes=1)

    def test_flushes_on_the_line_bound(self) -> None:
        batcher = StreamBatcher(max_lines=2, max_bytes=1_000_000)
        assert batcher.add(LABELS, LogEntry(1, "a")) is None
        batch = batcher.add(LABELS, LogEntry(2, "b"))
        assert batch is not None
        assert len(batch[0][1]) == 2
        assert len(batcher) == 0

    def test_flushes_on_the_byte_bound(self) -> None:
        batcher = StreamBatcher(max_lines=10_000, max_bytes=100)
        emitted = [
            batch for _, entry in pairs(20, size=50) if (batch := batcher.add(LABELS, entry))
        ]
        assert emitted, "a 50-byte line against a 100-byte bound must flush"

    def test_flush_on_an_empty_batcher_returns_none(self) -> None:
        assert StreamBatcher(max_lines=5, max_bytes=100).flush() is None

    def test_emits_an_oversized_single_entry_rather_than_dropping_it(self) -> None:
        """Dropping a customer's log line because it is large is worse than one
        oversized request; Loki says clearly if it refuses."""
        batcher = StreamBatcher(max_lines=1_000, max_bytes=10)
        batch = batcher.add(LABELS, LogEntry(1, "x" * 5_000))
        assert batch is not None
        assert batch[0][1][0].line.startswith("xxxxx")

    def test_drain_yields_every_batch_including_the_remainder(self) -> None:
        batcher = StreamBatcher(max_lines=3, max_bytes=1_000_000)
        batches = list(batcher.drain(pairs(7)))
        assert [len(entries) for _, entries in (b[0] for b in batches)] == [3, 3, 1]

    def test_drain_on_an_empty_iterator_yields_nothing(self) -> None:
        batcher = StreamBatcher(max_lines=3, max_bytes=1_000_000)
        assert list(batcher.drain(iter([]))) == []

    def test_drain_groups_by_label_set(self) -> None:
        batcher = StreamBatcher(max_lines=4, max_bytes=1_000_000)
        entries = iter(
            [
                ({"service_name": "a"}, LogEntry(1, "one")),
                ({"service_name": "b"}, LogEntry(2, "two")),
                ({"service_name": "a"}, LogEntry(3, "three")),
                ({"service_name": "b"}, LogEntry(4, "four")),
            ]
        )
        batches = list(batcher.drain(entries))
        assert len(batches) == 1
        assert len(batches[0]) == 2
