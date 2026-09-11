"""Pair an AEM request-log `->` line with its `<-` response line.

The AEM request log splits one HTTP request across two lines: the first carries
the method and path, the second carries the status and the duration. Neither is
useful alone, and "p99 latency by path" - the question anyone opens an AEM
dashboard to ask - needs both. Loki has no join, so no LogQL query can put them
back together. The pairing has to happen at write time.

The response line is the one that gets enriched, because it is the line carrying
the duration and therefore the line a latency panel selects.

## Why responses are buffered, with numbers

AEM writes the request line when a request starts and the response line when it
finishes, and the file is ordered by write time - so the two lines for one
request are usually adjacent but frequently inverted. The first two lines of the
sample request log are the response and then the request, for the same id, in
the same second.

Measured over one real author-tier day, 45824 lines and 22912 responses:

| strategy | responses paired |
|---|---|
| key on request id, no buffering | 66.1% |
| key on (node, request id), no buffering | 57.5% |
| either key, buffering unmatched responses | **100.0%** |

Two things in that table are worth keeping. Buffering is what fixes it, taking
a third of all latency samples from unusable to usable. And **scoping the key by
node made pairing worse, not better** - which is why it would be easy to "fix"
this by adding node scoping alone and ship a regression. Node scoping is still
applied, because with buffering it costs nothing (100.0% either way) and it
removes the chance of pairing a response with a different pod's request that
happened to reuse the id. AEM request ids are per-instance counters that wrap,
and 11169 ids in that one day appeared on more than one pod.

## What this deliberately does not do

It does not correlate across objects. A request whose response lands in the next
hour's file is never paired, and fixing that would need external state or a
Lambda held open. The counters report the gap instead, and an unpaired response
carries `correlated="false"` so a panel can show it rather than assume it away.

A buffered response is emitted **later than its position in the file**. That is
safe: Loki has accepted out-of-order writes within a stream since 2.4, and the
entry keeps its own parsed timestamp, so ordering in Loki is by event time
regardless of push order.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .parsers import ParsedRecord

# Fields copied from the request line onto its response line. Not the whole
# metadata dict: `node_id` and `request_id` are already on both, and copying
# `direction` would overwrite the thing that distinguishes them.
_CARRIED_FIELDS = ("method", "path", "protocol")

_CorrelationKey = tuple[str, str]


@dataclass(slots=True)
class CorrelationStats:
    """Counters worth logging once per object.

    `evicted` being non-zero means `max_pending` was too small for this object,
    which shows up as response lines missing their method and path. Silent
    truncation of a bounded cache is exactly the kind of thing that gets
    diagnosed as "the parser is broken" six months later, so it is counted.
    """

    paired: int = 0
    unpaired_responses: int = 0
    evicted: int = 0


class RequestCorrelator:
    """Best-effort in-object pairing of request and response lines.

    Bounded on both sides. An AEM request log with a million lines - a 600
    second Lambda reading a large object is where that happens - must not grow a
    dict until the function is OOM-killed mid-object, because that loses every
    line already shipped. At the cap the oldest pending entry is dropped and
    counted.
    """

    __slots__ = ("_max_pending", "_pending_requests", "_pending_responses", "stats")

    def __init__(self, max_pending: int = 20_000) -> None:
        if max_pending < 1:
            raise ValueError("max_pending must be at least 1")
        self._max_pending = max_pending
        # Insertion-ordered so eviction is oldest-first. A plain dict preserves
        # insertion order too, but `popitem(last=False)` is the operation this
        # needs and OrderedDict is the type that says so.
        self._pending_requests: OrderedDict[_CorrelationKey, dict[str, str]] = OrderedDict()
        self._pending_responses: OrderedDict[_CorrelationKey, ParsedRecord] = OrderedDict()
        self.stats = CorrelationStats()

    def process(self, record: ParsedRecord) -> list[ParsedRecord]:
        """Feed one record in, get zero or more records out.

        Zero means the record is a response being held until its request shows
        up. Two means this record is a request that released a held response.
        Anything that is not a request-log line passes straight through, so this
        can sit in the pipeline unconditionally.
        """
        key = self._key(record)
        if key is None:
            return [record]
        direction = record.metadata["direction"]
        return (
            self._on_request(key, record)
            if direction == "request"
            else self._on_response(key, record)
        )

    def flush(self) -> list[ParsedRecord]:
        """Emit every response still waiting for a request.

        Called once at the end of an object. These are the genuinely unpairable
        ones - their request line is in a different file - and they ship with
        `correlated="false"` rather than being dropped.
        """
        released = [self._mark_uncorrelated(record) for record in self._pending_responses.values()]
        self.stats.unpaired_responses += len(released)
        self._pending_responses.clear()
        self._pending_requests.clear()
        return released

    # --- internals -----------------------------------------------------------

    @staticmethod
    def _key(record: ParsedRecord) -> _CorrelationKey | None:
        metadata = record.metadata
        request_id = metadata.get("request_id")
        direction = metadata.get("direction")
        if not request_id or direction not in ("request", "response"):
            return None
        # An absent node id collapses to one bucket, which is the pre-existing
        # behaviour and still correct for a single-pod environment.
        return (metadata.get("node_id", ""), request_id)

    def _on_request(self, key: _CorrelationKey, record: ParsedRecord) -> list[ParsedRecord]:
        carried = {
            name: record.metadata[name] for name in _CARRIED_FIELDS if name in record.metadata
        }
        held = self._pending_responses.pop(key, None)
        if held is not None:
            self.stats.paired += 1
            # The request line first, so the file's own order is preserved for
            # the pair that this release reconstructs.
            return [record, self._enrich(held, carried)]
        if carried:
            self._remember_request(key, carried)
        return [record]

    def _on_response(self, key: _CorrelationKey, record: ParsedRecord) -> list[ParsedRecord]:
        pending = self._pending_requests.pop(key, None)
        if pending is not None:
            self.stats.paired += 1
            return [self._enrich(record, pending)]
        # Held for a request that may still be coming. The buffer is bounded, so
        # holding this one can force out the oldest held response - which is
        # emitted now, uncorrelated, rather than dropped. No log line is ever
        # lost to a full buffer; only its enrichment is.
        evicted = self._hold_response(key, record)
        return [] if evicted is None else [evicted]

    @staticmethod
    def _enrich(record: ParsedRecord, carried: dict[str, str]) -> ParsedRecord:
        return ParsedRecord(
            message=record.message,
            timestamp_ns=record.timestamp_ns,
            level=record.level,
            metadata={**record.metadata, **carried, "correlated": "true"},
        )

    @staticmethod
    def _mark_uncorrelated(record: ParsedRecord) -> ParsedRecord:
        return ParsedRecord(
            message=record.message,
            timestamp_ns=record.timestamp_ns,
            level=record.level,
            metadata={**record.metadata, "correlated": "false"},
        )

    def _remember_request(self, key: _CorrelationKey, carried: dict[str, str]) -> None:
        if key in self._pending_requests:
            # A wrapped request id on the same pod. The newer request is the
            # better guess for the next response.
            self._pending_requests.pop(key)
        elif len(self._pending_requests) >= self._max_pending:
            self._pending_requests.popitem(last=False)
            self.stats.evicted += 1
        self._pending_requests[key] = carried

    def _hold_response(self, key: _CorrelationKey, record: ParsedRecord) -> ParsedRecord | None:
        """Buffer a response, returning whichever one this displaced, if any.

        Two things can displace a held response, and both must return it rather
        than overwrite it - this promises never to lose a log line.

        A **duplicate key** is the one that is easy to miss. AEM request ids are
        per-instance counters that wrap, so two responses on the same pod can
        share an id with no request line between them. Assigning straight into
        the dict dropped the older one silently, and the
        `no line is ever lost` test used unique ids so it did not notice.
        """
        displaced: ParsedRecord | None = None
        existing = self._pending_responses.pop(key, None)
        if existing is not None:
            self.stats.unpaired_responses += 1
            displaced = self._mark_uncorrelated(existing)
        elif len(self._pending_responses) >= self._max_pending:
            _, oldest = self._pending_responses.popitem(last=False)
            self.stats.evicted += 1
            self.stats.unpaired_responses += 1
            displaced = self._mark_uncorrelated(oldest)
        self._pending_responses[key] = record
        return displaced

    @property
    def pending(self) -> int:
        """Lines still buffered when the object ended."""
        return len(self._pending_requests) + len(self._pending_responses)
