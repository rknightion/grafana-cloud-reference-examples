"""Request/response pairing.

The measured behaviour these tests pin down: buffering unmatched responses takes
pairing from 66% to 100% on a real author-tier day, and node-scoping the key
*without* buffering makes it worse, not better. Both numbers are in the
`correlate` module docstring.
"""

from __future__ import annotations

from adobe_aem.correlate import RequestCorrelator
from adobe_aem.parsers import ParsedRecord, parse_request


def request_line(request_id: str, path: str = "/a.html", node: str = "node-a") -> ParsedRecord:
    return parse_request(
        f"24/Jul/2026:00:00:04 +0000 [{request_id}] -> GET {path} HTTP/1.1 [{node}]"
    )


def response_line(request_id: str, duration: int = 12, node: str = "node-a") -> ParsedRecord:
    return parse_request(
        f"24/Jul/2026:00:00:04 +0000 [{request_id}] <- 200 text/html {duration}ms [{node}]"
    )


class TestOrdering:
    def test_request_then_response(self) -> None:
        correlator = RequestCorrelator()
        assert len(correlator.process(request_line("1"))) == 1
        out = correlator.process(response_line("1"))
        assert len(out) == 1
        assert out[0].metadata["path"] == "/a.html"
        assert out[0].metadata["method"] == "GET"
        assert out[0].metadata["correlated"] == "true"
        assert correlator.stats.paired == 1

    def test_response_then_request_still_pairs(self) -> None:
        """The case that buffering exists for.

        AEM writes the two lines from different threads, so the response
        genuinely arrives first - the very first two lines of the real fixture
        are a response followed by its request.
        """
        correlator = RequestCorrelator()
        # Held back: nothing to emit yet.
        assert correlator.process(response_line("1")) == []
        out = correlator.process(request_line("1"))
        # Both come out now, request first so the file's own order is kept.
        assert len(out) == 2
        assert out[0].metadata["direction"] == "request"
        assert out[1].metadata["direction"] == "response"
        assert out[1].metadata["path"] == "/a.html"
        assert out[1].metadata["correlated"] == "true"
        assert correlator.stats.paired == 1

    def test_two_responses_sharing_a_key_both_come_out(self) -> None:
        """The duplicate-key case, which unique-id tests cannot reach.

        AEM request ids are per-instance counters that wrap, so two responses on
        one pod can share an id with no request line between them. The second
        used to overwrite the first in the buffer and the first was never
        emitted - a silently lost log line.
        """
        correlator = RequestCorrelator()
        assert correlator.process(response_line("9", duration=11)) == []
        displaced = correlator.process(response_line("9", duration=22))
        assert len(displaced) == 1
        assert displaced[0].metadata["duration_ms"] == "11"
        assert displaced[0].metadata["correlated"] == "false"
        released = correlator.flush()
        assert len(released) == 1
        assert released[0].metadata["duration_ms"] == "22"

    def test_no_line_is_ever_lost(self) -> None:
        """Every input line comes out exactly once, across process() and flush()."""
        correlator = RequestCorrelator()
        emitted = 0
        fed = 0
        for index in range(50):
            # Interleaved out of order, and reusing ids across iterations, so
            # the count covers repeated keys as well as inverted pairs.
            # It does NOT cover two responses buffered under one key with no
            # request between them - each pair here resolves immediately. That
            # case has its own test above, because this one passed throughout
            # the window where such a response was silently dropped.
            for request_id in (str(index), str(index % 7)):
                fed += 2
                emitted += len(correlator.process(response_line(request_id)))
                emitted += len(correlator.process(request_line(request_id)))
        emitted += len(correlator.flush())
        assert emitted == fed


class TestUnpaired:
    def test_response_with_no_request_is_flushed_as_uncorrelated(self) -> None:
        correlator = RequestCorrelator()
        assert correlator.process(response_line("orphan")) == []
        released = correlator.flush()
        assert len(released) == 1
        assert released[0].metadata["correlated"] == "false"
        assert "path" not in released[0].metadata
        assert correlator.stats.unpaired_responses == 1

    def test_request_with_no_response_is_emitted_immediately_and_not_again(self) -> None:
        correlator = RequestCorrelator()
        assert len(correlator.process(request_line("lonely"))) == 1
        assert correlator.flush() == []

    def test_flush_is_idempotent(self) -> None:
        correlator = RequestCorrelator()
        correlator.process(response_line("orphan"))
        assert len(correlator.flush()) == 1
        assert correlator.flush() == []


class TestNodeScoping:
    def test_two_pods_reusing_one_request_id_do_not_cross_pair(self) -> None:
        """AEM request ids are per-instance counters that wrap.

        11169 ids appeared on more than one pod in a single real day, so an
        unscoped key can attach pod B's path to pod A's duration.
        """
        correlator = RequestCorrelator()
        correlator.process(request_line("500", path="/from-a.html", node="node-a"))
        correlator.process(request_line("500", path="/from-b.html", node="node-b"))
        from_b = correlator.process(response_line("500", node="node-b"))
        from_a = correlator.process(response_line("500", node="node-a"))
        assert from_b[0].metadata["path"] == "/from-b.html"
        assert from_a[0].metadata["path"] == "/from-a.html"

    def test_a_repeated_id_on_one_pod_keeps_the_newer_request(self) -> None:
        correlator = RequestCorrelator()
        correlator.process(request_line("7", path="/old.html"))
        correlator.process(request_line("7", path="/new.html"))
        out = correlator.process(response_line("7"))
        assert out[0].metadata["path"] == "/new.html"


class TestBounds:
    def test_pending_requests_are_capped_and_the_eviction_is_counted(self) -> None:
        correlator = RequestCorrelator(max_pending=2)
        for index in range(5):
            correlator.process(request_line(str(index)))
        assert correlator.stats.evicted == 3
        # The oldest requests were dropped, so response 0 has nothing to pair
        # with. It is buffered rather than emitted, and comes out of flush()
        # honestly marked uncorrelated.
        assert correlator.process(response_line("0")) == []
        released = correlator.flush()
        assert len(released) == 1
        assert released[0].metadata["correlated"] == "false"

    def test_evicting_a_held_response_emits_it_rather_than_dropping_it(self) -> None:
        """A full buffer costs enrichment, never a log line."""
        correlator = RequestCorrelator(max_pending=1)
        assert correlator.process(response_line("1")) == []
        displaced = correlator.process(response_line("2"))
        assert len(displaced) == 1
        assert displaced[0].metadata["request_id"] == "1"
        assert displaced[0].metadata["correlated"] == "false"
        assert correlator.stats.evicted == 1

    def test_zero_max_pending_is_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="at least 1"):
            RequestCorrelator(max_pending=0)


class TestPassthrough:
    def test_a_non_request_log_record_passes_straight_through(self) -> None:
        """So the correlator can sit in the pipeline without a log-type branch."""
        correlator = RequestCorrelator()
        record = ParsedRecord(message="an access log line", metadata={"status": "200"})
        assert correlator.process(record) == [record]

    def test_a_record_with_no_request_id_passes_through(self) -> None:
        correlator = RequestCorrelator()
        record = ParsedRecord(message="x", metadata={"direction": "response"})
        assert correlator.process(record) == [record]
