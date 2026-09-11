"""Tests for generic-s3.

Scoped to what is specific to this example: label derivation, timestamp
extraction and the key-suffix skip. The S3 reading, batching, Loki push and
partial-failure handling are covered by common/python/tests and are not retested
here - a copy in every example makes changing the shared code expensive for no
extra coverage.
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Protocol

import pytest

from grafana_cloud_common.aws.s3 import S3ObjectReader, S3ObjectRef
from grafana_cloud_common.loki import LokiClient, validate_labels
from grafana_cloud_common.testing import FakeOpener, FakeS3Client


class LoadHandler(Protocol):
    def __call__(self, **environment: str) -> Any: ...


REF = S3ObjectRef(bucket="acme-logs", key="team/app/2026-09-11/app.log.gz", size_bytes=42)


class TestLabels:
    def test_the_default_label_set_is_valid(self, load_handler: LoadHandler) -> None:
        """validate_labels is the same check the client applies before a push, so
        a cardinality mistake here fails the test rather than the bill."""
        handler = load_handler()
        labels = handler.labels_for(REF)
        validate_labels(labels)
        assert labels == {"service_name": "generic-s3", "bucket": "acme-logs"}

    def test_service_name_is_configurable(self, load_handler: LoadHandler) -> None:
        handler = load_handler(SERVICE_NAME="acme-app-logs")
        assert handler.labels_for(REF)["service_name"] == "acme-app-logs"

    def test_no_label_carries_the_object_key(self, load_handler: LoadHandler) -> None:
        """The key is high-cardinality: one Loki stream per file. It belongs in
        structured metadata, which _entries puts it in."""
        handler = load_handler(PREFIX_LABEL_DEPTH="2")
        assert REF.key not in handler.labels_for(REF).values()

    def test_prefix_label_takes_the_configured_depth(self, load_handler: LoadHandler) -> None:
        handler = load_handler(PREFIX_LABEL_DEPTH="2")
        labels = handler.labels_for(REF)
        assert labels["prefix"] == "team/app"
        validate_labels(labels)

    def test_prefix_label_is_absent_by_default(self, load_handler: LoadHandler) -> None:
        assert "prefix" not in load_handler().labels_for(REF)

    def test_a_prefix_deeper_than_the_key_is_omitted_rather_than_truncated(
        self, load_handler: LoadHandler
    ) -> None:
        """A partial prefix would put two different layouts in one stream, which
        is worse than having no prefix label at all."""
        handler = load_handler(PREFIX_LABEL_DEPTH="5")
        assert "prefix" not in handler.labels_for(S3ObjectRef("acme-logs", "a/b.log"))

    def test_static_labels_reach_the_push_but_not_labels_for(
        self, load_handler: LoadHandler
    ) -> None:
        """Static labels are merged by the client, not by the handler, so they
        apply to every example uniformly."""
        handler = load_handler(LOKI_STATIC_LABELS='{"env":"prod"}')
        assert "env" not in handler.labels_for(REF)
        assert handler.CONFIG.static_labels == {"env": "prod"}


class TestTimestampExtraction:
    def test_ingestion_time_is_the_default(self, load_handler: LoadHandler) -> None:
        """Loki rejects samples older than the tenant's window with a 400 that no
        retry fixes, so extraction is opt-in."""
        handler = load_handler()
        assert handler._timestamp_for('{"ts":"2020-01-01T00:00:00Z"}', 999) == 999

    def test_rfc3339(self, load_handler: LoadHandler) -> None:
        handler = load_handler(TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="rfc3339")
        assert (
            handler._timestamp_for('{"ts":"2026-09-11T00:00:00Z"}', 1) == 1_789_084_800_000_000_000
        )

    def test_rfc3339_without_a_timezone_falls_back(self, load_handler: LoadHandler) -> None:
        """A naive timestamp is not an absolute instant, so guessing a zone would
        silently shift every line by hours."""
        handler = load_handler(TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="rfc3339")
        assert handler._timestamp_for('{"ts":"2026-09-11T00:00:00"}', 42) == 42

    @pytest.mark.parametrize(
        ("fmt", "value", "expected"),
        [
            ("epoch_s", 1, 1_000_000_000),
            ("epoch_ms", 1, 1_000_000),
            ("epoch_us", 1, 1_000),
            ("epoch_ns", 1, 1),
        ],
    )
    def test_epoch_scales(
        self, load_handler: LoadHandler, fmt: str, value: int, expected: int
    ) -> None:
        handler = load_handler(TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT=fmt)
        assert handler._timestamp_for(f'{{"ts":{value}}}', 0) == expected

    def test_epoch_ns_keeps_full_precision(self, load_handler: LoadHandler) -> None:
        """Via float() this would round: a nanosecond epoch is 19 digits and a
        double carries about 16."""
        handler = load_handler(TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="epoch_ns")
        assert handler._timestamp_for('{"ts":1789084800123456789}', 0) == 1789084800123456789

    def test_infinity_falls_back_instead_of_raising(self, load_handler: LoadHandler) -> None:
        """int(float("inf")) raises OverflowError, not ValueError, and Infinity is
        valid JSON to Python's decoder."""
        handler = load_handler(TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="epoch_s")
        assert handler._timestamp_for('{"ts":Infinity}', 7) == 7

    def test_a_missing_field_falls_back(self, load_handler: LoadHandler) -> None:
        handler = load_handler(TIMESTAMP_FIELD="ts")
        assert handler._timestamp_for('{"other":1}', 7) == 7

    def test_a_non_json_line_falls_back(self, load_handler: LoadHandler) -> None:
        handler = load_handler(TIMESTAMP_FIELD="ts")
        assert handler._timestamp_for("plain text line", 7) == 7

    def test_an_over_age_timestamp_falls_back_to_ingestion_time(
        self, load_handler: LoadHandler
    ) -> None:
        """Better one line stamped 'now' than a 400 that rejects the whole batch."""
        handler = load_handler(
            TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="epoch_s", MAX_TIMESTAMP_AGE_SECONDS="60"
        )
        now_ns = 1_000_000 * 1_000_000_000
        old = '{"ts":1}'
        assert handler._timestamp_for(old, now_ns) == now_ns

    def test_a_within_age_timestamp_is_kept(self, load_handler: LoadHandler) -> None:
        handler = load_handler(
            TIMESTAMP_FIELD="ts", TIMESTAMP_FORMAT="epoch_s", MAX_TIMESTAMP_AGE_SECONDS="600"
        )
        now_ns = 1_000 * 1_000_000_000
        assert handler._timestamp_for('{"ts":999}', now_ns) == 999_000_000_000


class TestConfigValidation:
    def test_a_negative_prefix_depth_is_rejected_at_import(self, load_handler: LoadHandler) -> None:
        from grafana_cloud_common.errors import ConfigError

        with pytest.raises(ConfigError, match="zero or positive"):
            load_handler(PREFIX_LABEL_DEPTH="-1")

    def test_an_unknown_record_format_is_rejected_at_import(
        self, load_handler: LoadHandler
    ) -> None:
        from grafana_cloud_common.errors import ConfigError

        with pytest.raises(ConfigError, match="RECORD_FORMAT"):
            load_handler(RECORD_FORMAT="parquet")

    def test_auto_is_accepted(self, load_handler: LoadHandler) -> None:
        assert load_handler(RECORD_FORMAT="auto")._resolved_format() is None

    def test_an_explicit_format_overrides_the_key_suffix(self, load_handler: LoadHandler) -> None:
        from grafana_cloud_common.aws.s3 import RecordFormat

        handler = load_handler(RECORD_FORMAT="jsonl")
        assert handler._resolved_format() is RecordFormat.JSONL


def wire(handler: Any, payload: bytes, *, gzipped: bool = False) -> FakeOpener:
    """Give the module a fake S3 and a scripted Loki, and return the Loki double.

    Replaces the module-scope singletons rather than patching internals: that is
    the seam the real deployment uses too, so the test exercises the same path.
    """
    opener = FakeOpener([204] * 20)
    handler.READER = S3ObjectReader(client=FakeS3Client(payload, gzipped=gzipped))
    handler.CLIENT = LokiClient(handler.CONFIG, lambda: "glc_fake", opener=opener)
    return opener


def pushed(opener: FakeOpener, index: int = 0) -> Any:
    """The decoded push body. The default config gzips, so the real wire format
    is what gets asserted rather than a special-cased uncompressed one."""
    recorded = opener.recorded[index]
    raw = recorded.body
    if recorded.headers.get("content-encoding") == "gzip":
        raw = gzip.decompress(raw)
    return json.loads(raw)


class TestShipObject:
    def test_a_zero_byte_object_is_skipped_without_a_get(self, load_handler: LoadHandler) -> None:
        handler = load_handler()
        wire(handler, b"should not be read\n")
        assert handler.ship_object(S3ObjectRef("acme-logs", "empty.log", size_bytes=0)) == 0

    def test_a_key_not_matching_the_suffix_is_skipped(self, load_handler: LoadHandler) -> None:
        """An EventBridge rule cannot AND a suffix onto a prefix, so the filter is
        enforced here for that wiring."""
        handler = load_handler(SOURCE_KEY_SUFFIX=".gz")
        wire(handler, b"a\nb\n")
        assert handler.ship_object(S3ObjectRef("acme-logs", "logs/app.log")) == 0

    def test_a_matching_suffix_is_shipped(self, load_handler: LoadHandler) -> None:
        handler = load_handler(SOURCE_KEY_SUFFIX=".gz")
        opener = wire(handler, b"first\nsecond\n", gzipped=True)
        shipped = handler.ship_object(S3ObjectRef("acme-logs", "logs/app.log.gz", size_bytes=10))
        assert shipped == 2
        assert len(opener.recorded) == 1

    def test_the_object_key_and_record_number_land_in_structured_metadata(
        self, load_handler: LoadHandler
    ) -> None:
        """The whole point of the label discipline: provenance stays queryable
        without creating a stream per file."""
        handler = load_handler()
        opener = wire(handler, b"only line\n")
        handler.ship_object(S3ObjectRef("acme-logs", "logs/a.log", size_bytes=10))

        stream = pushed(opener)["streams"][0]
        assert stream["stream"] == {"service_name": "generic-s3", "bucket": "acme-logs"}
        assert stream["values"][0][2] == {"object_key": "logs/a.log", "record": "1"}

    def test_the_object_version_is_recorded_when_present(self, load_handler: LoadHandler) -> None:
        handler = load_handler()
        opener = wire(handler, b"a\n")
        handler.ship_object(S3ObjectRef("acme-logs", "logs/a.log", size_bytes=10, version_id="v-1"))
        metadata = pushed(opener)["streams"][0]["values"][0][2]
        assert metadata["object_version_id"] == "v-1"

    def test_jsonl_is_reemitted_compactly(self, load_handler: LoadHandler) -> None:
        handler = load_handler()
        opener = wire(handler, b'{"a": 1,  "b": 2}\n')
        assert handler.ship_object(S3ObjectRef("acme-logs", "logs/a.jsonl", size_bytes=10)) == 1
        line = pushed(opener)["streams"][0]["values"][0][1]
        assert line == '{"a":1,"b":2}'


class TestLambdaHandler:
    def test_an_event_with_no_objects_returns_an_empty_failure_list(
        self, load_handler: LoadHandler
    ) -> None:
        handler = load_handler()

        class Context:
            aws_request_id = "req-1"

            def get_remaining_time_in_millis(self) -> int:
                return 300_000

        body = {"Service": "Amazon S3", "Event": "s3:TestEvent"}
        event = {"Records": [{"eventSource": "aws:sqs", "messageId": "m-1", "body": str(body)}]}
        # A str()'d dict is not JSON, so this is the poison-message path: reported
        # as failed rather than silently acknowledged.
        assert handler.lambda_handler(event, Context()) == {
            "batchItemFailures": [{"itemIdentifier": "m-1"}]
        }

    def test_a_genuine_test_event_is_acknowledged(self, load_handler: LoadHandler) -> None:
        handler = load_handler()

        class Context:
            aws_request_id = "req-1"

            def get_remaining_time_in_millis(self) -> int:
                return 300_000

        body = json.dumps({"Service": "Amazon S3", "Event": "s3:TestEvent"})
        event = {"Records": [{"eventSource": "aws:sqs", "messageId": "m-1", "body": body}]}
        assert handler.lambda_handler(event, Context()) == {"batchItemFailures": []}
