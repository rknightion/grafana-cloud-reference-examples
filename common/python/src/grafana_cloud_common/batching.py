"""Accumulate log entries and flush them as bounded batches.

A Lambda reading a 500 MB gzipped object cannot hold every line in memory, and
pushing one line per request is both slow and rate-limited. This sits between:
entries go in, full batches come out, and the caller decides what to do with each.

Bounded by line count and by approximate byte size, and both matter. 5,000 tiny
lines is a fine batch; 5,000 lines of a 200 KB JSON blob each is not.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from .loki import LogEntry, Stream, group_by_labels


class StreamBatcher:
    """Collects (labels, entry) pairs and yields batches at the configured bounds.

    Not thread-safe. A Lambda invocation is single-threaded per container; if you
    add concurrency, batch per worker rather than sharing one of these.
    """

    __slots__ = ("_bytes", "_entries", "_max_bytes", "_max_lines")

    def __init__(self, *, max_lines: int, max_bytes: int) -> None:
        if max_lines <= 0 or max_bytes <= 0:
            raise ValueError("max_lines and max_bytes must both be positive")
        self._max_lines = max_lines
        self._max_bytes = max_bytes
        self._entries: list[tuple[Mapping[str, str], LogEntry]] = []
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def pending_bytes(self) -> int:
        return self._bytes

    def add(self, labels: Mapping[str, str], entry: LogEntry) -> list[Stream] | None:
        """Add one entry. Returns a batch when a bound is reached, else None.

        A single entry larger than max_bytes is still emitted on its own rather
        than rejected - dropping a customer's log line because it is big is worse
        than one oversized request, and Loki will say so clearly if it refuses.
        """
        self._entries.append((labels, entry))
        self._bytes += entry.approximate_bytes()
        if len(self._entries) >= self._max_lines or self._bytes >= self._max_bytes:
            return self.flush()
        return None

    def flush(self) -> list[Stream] | None:
        """Emit whatever is buffered. Returns None when empty, so the caller can
        treat the end-of-input flush identically to a mid-stream one."""
        if not self._entries:
            return None
        batch = group_by_labels(self._entries)
        self._entries = []
        self._bytes = 0
        return batch

    def drain(self, pairs: Iterator[tuple[Mapping[str, str], LogEntry]]) -> Iterator[list[Stream]]:
        """Consume an iterator of pairs and yield every batch, including the last.

        The common shape in a handler:

            for batch in batcher.drain(reader.entries()):
                client.push(batch)
        """
        for labels, entry in pairs:
            batch = self.add(labels, entry)
            if batch is not None:
                yield batch
        final = self.flush()
        if final is not None:
            yield final
