from __future__ import annotations

import gzip
import json
from dataclasses import replace

import pytest

from grafana_cloud_common.config import LokiConfig
from grafana_cloud_common.errors import PermanentError, RetryableError
from grafana_cloud_common.loki import (
    LogEntry,
    LokiClient,
    group_by_labels,
    validate_labels,
)
from grafana_cloud_common.testing import FakeOpener, http_error


def client(config: LokiConfig, opener: FakeOpener) -> LokiClient:
    return LokiClient(config, lambda: "glc_fake", opener=opener)


class TestValidateLabels:
    def test_accepts_a_low_cardinality_set(self) -> None:
        validate_labels({"service_name": "generic-s3", "bucket": "acme-logs"})

    def test_rejects_an_empty_set(self) -> None:
        with pytest.raises(PermanentError, match="at least one label"):
            validate_labels({})

    @pytest.mark.parametrize("name", ["object_key", "request_id", "filename", "ID", "TraceID"])
    def test_rejects_banned_high_cardinality_names(self, name: str) -> None:
        with pytest.raises(PermanentError, match="high-cardinality"):
            validate_labels({name: "anything"})

    @pytest.mark.parametrize("name", ["9lives", "has-a-dash", "has.a.dot", ""])
    def test_rejects_names_loki_will_refuse(self, name: str) -> None:
        with pytest.raises(PermanentError, match="not valid Loki syntax"):
            validate_labels({name: "value"})

    def test_rejects_an_empty_value(self) -> None:
        with pytest.raises(PermanentError, match="empty value"):
            validate_labels({"service_name": ""})

    def test_rejects_too_many_labels(self) -> None:
        labels = {f"label_{index}": "v" for index in range(20)}
        with pytest.raises(PermanentError, match="label ceiling"):
            validate_labels(labels)


class TestBuildPayload:
    def test_sorts_entries_ascending_within_a_stream(self, config: LokiConfig) -> None:
        subject = client(config, FakeOpener([]))
        labels = {"service_name": "x"}
        entries = [LogEntry(300, "third"), LogEntry(100, "first"), LogEntry(200, "second")]

        body = json.loads(subject.build_payload([(labels, entries)]))

        values = body["streams"][0]["values"]
        assert [value[0] for value in values] == ["100", "200", "300"]
        assert [value[1] for value in values] == ["first", "second", "third"]

    def test_omits_the_metadata_slot_when_there_is_none(self, config: LokiConfig) -> None:
        subject = client(config, FakeOpener([]))
        body = json.loads(subject.build_payload([({"service_name": "x"}, [LogEntry(1, "a")])]))
        assert body["streams"][0]["values"] == [["1", "a"]]

    def test_emits_structured_metadata_as_the_third_slot(self, config: LokiConfig) -> None:
        subject = client(config, FakeOpener([]))
        entry = LogEntry(1, "a", {"object_key": "logs/a.gz"})
        body = json.loads(subject.build_payload([({"service_name": "x"}, [entry])]))
        assert body["streams"][0]["values"] == [["1", "a", {"object_key": "logs/a.gz"}]]

    def test_merges_streams_with_identical_labels_regardless_of_key_order(
        self, config: LokiConfig
    ) -> None:
        subject = client(config, FakeOpener([]))
        body = json.loads(
            subject.build_payload(
                [
                    ({"a": "1", "b": "2"}, [LogEntry(1, "first")]),
                    ({"b": "2", "a": "1"}, [LogEntry(2, "second")]),
                ]
            )
        )
        assert len(body["streams"]) == 1
        assert len(body["streams"][0]["values"]) == 2

    def test_static_labels_are_merged_and_overridable(self, config: LokiConfig) -> None:
        configured = replace(config, static_labels={"env": "prod", "service_name": "default"})
        subject = client(configured, FakeOpener([]))
        body = json.loads(
            subject.build_payload([({"service_name": "override"}, [LogEntry(1, "a")])])
        )
        assert body["streams"][0]["stream"] == {"env": "prod", "service_name": "override"}


class TestPush:
    def test_returns_the_line_count_on_success(self, config: LokiConfig) -> None:
        opener = FakeOpener([204])
        assert client(config, opener).push([({"service_name": "x"}, [LogEntry(1, "a")])]) == 1
        assert opener.recorded[0].headers["authorization"].startswith("Basic ")

    def test_does_nothing_for_an_empty_batch(self, config: LokiConfig) -> None:
        opener = FakeOpener([])
        assert client(config, opener).push([]) == 0
        assert opener.recorded == []

    def test_gzips_when_configured(self, config: LokiConfig) -> None:
        opener = FakeOpener([204])
        client(replace(config, compress=True), opener).push(
            [({"service_name": "x"}, [LogEntry(1, "a")])]
        )
        recorded = opener.recorded[0]
        assert recorded.headers["content-encoding"] == "gzip"
        assert json.loads(gzip.decompress(recorded.body))["streams"]

    def test_retries_a_429_then_succeeds(self, config: LokiConfig) -> None:
        opener = FakeOpener([http_error(429, retry_after="0"), 204])
        assert client(config, opener).push([({"service_name": "x"}, [LogEntry(1, "a")])]) == 1
        assert len(opener.recorded) == 2

    def test_gives_up_after_the_retry_budget(self, config: LokiConfig) -> None:
        opener = FakeOpener([http_error(503, retry_after="0") for _ in range(3)])
        with pytest.raises(RetryableError, match="after 3 attempts"):
            client(config, opener).push([({"service_name": "x"}, [LogEntry(1, "a")])])
        assert len(opener.recorded) == 3

    def test_a_400_is_permanent_and_not_retried(self, config: LokiConfig) -> None:
        opener = FakeOpener([http_error(400, body=b"entry out of order")])
        with pytest.raises(PermanentError) as caught:
            client(config, opener).push([({"service_name": "x"}, [LogEntry(1, "a")])])
        assert caught.value.status == 400
        assert len(opener.recorded) == 1

    def test_a_401_refreshes_the_credential_once_then_gives_up(self, config: LokiConfig) -> None:
        """A rotated token should recover; a wrong access-policy scope should not
        burn the whole retry budget."""
        tokens = iter(["stale", "fresh"])
        opener = FakeOpener([http_error(401), http_error(401)])
        subject = LokiClient(config, lambda: next(tokens), opener=opener)

        with pytest.raises(PermanentError, match="logs:write"):
            subject.push([({"service_name": "x"}, [LogEntry(1, "a")])])

        assert len(opener.recorded) == 2
        assert (
            opener.recorded[0].headers["authorization"]
            != opener.recorded[1].headers["authorization"]
        )

    def test_a_banned_label_fails_before_any_request(self, config: LokiConfig) -> None:
        opener = FakeOpener([])
        with pytest.raises(PermanentError, match="high-cardinality"):
            client(config, opener).push([({"object_key": "a/b.gz"}, [LogEntry(1, "a")])])
        assert opener.recorded == []


class TestGroupByLabels:
    def test_collapses_pairs_into_streams(self) -> None:
        grouped = group_by_labels(
            [
                ({"service_name": "a"}, LogEntry(1, "one")),
                ({"service_name": "b"}, LogEntry(2, "two")),
                ({"service_name": "a"}, LogEntry(3, "three")),
            ]
        )
        assert len(grouped) == 2
        by_service = {labels["service_name"]: entries for labels, entries in grouped}
        assert [entry.line for entry in by_service["a"]] == ["one", "three"]
        assert [entry.line for entry in by_service["b"]] == ["two"]
