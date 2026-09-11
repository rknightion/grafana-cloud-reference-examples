"""Handler tests: S3 object in, Loki push out, with no AWS and no network.

`FakeS3Client` and `FakeOpener` come from `grafana_cloud_common.testing` and are
injected into the real reader and the real client, so these exercise the actual
request-building, batching and retry paths rather than mocks of them.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import pytest

from grafana_cloud_common import LokiClient
from grafana_cloud_common.aws import S3ObjectReader, S3ObjectRef
from grafana_cloud_common.testing import FakeOpener, FakeS3Client

ACCESS_LINES = "\n".join(
    [
        "cm-p12345-e67890-aem-author-abc123def-qs632 203.0.113.29 - "
        '24/Jul/2026:00:00:04 +0000 "HEAD /systemready HTTP/1.1" 200 - "-" "Java/21"',
        "cm-p12345-e67890-aem-author-abc123def-6liqc 203.0.113.30 - "
        '24/Jul/2026:00:00:11 +0000 "GET /missing.html HTTP/1.1" 404 512 "-" "curl/8.6.0"',
    ]
)
REQUEST_LINES = "\n".join(
    [
        "24/Jul/2026:00:00:04 +0000 [17722] <- 200 application/json 42ms [node-a]",
        "24/Jul/2026:00:00:04 +0000 [17722] -> GET /systemready HTTP/1.1 [node-a]",
        "24/Jul/2026:00:00:09 +0000 [17999] <- 500 text/html 9001ms [node-a]",
    ]
)


def wire(module: Any, payload: str, *, gzipped: bool = False) -> FakeOpener:
    """Point the handler's reader and client at fakes.

    The module resolves both at import, so a test replaces the instances rather
    than patching the classes - which keeps the real code paths in play.
    """
    body = payload.encode()
    module.READER = S3ObjectReader(client=FakeS3Client(gzip.compress(body) if gzipped else body))
    opener = FakeOpener([204] * 40)
    module.CLIENT = LokiClient(module.CONFIG, lambda: "glc_fake", opener=opener)
    return opener


def pushed_streams(opener: FakeOpener) -> list[dict[str, Any]]:
    streams: list[dict[str, Any]] = []
    for request in opener.recorded:
        body = request.body
        if isinstance(body, bytes) and request.headers.get("content-encoding") == "gzip":
            body = gzip.decompress(body)
        streams.extend(json.loads(body)["streams"])
    return streams


def entries(opener: FakeOpener) -> list[tuple[dict[str, str], str, dict[str, str]]]:
    """Flatten every pushed entry to (labels, line, metadata)."""
    flat = []
    for stream in pushed_streams(opener):
        for entry in stream["values"]:
            metadata = entry[2] if len(entry) > 2 else {}
            flat.append((stream["stream"], entry[1], metadata))
    return flat


class TestLabels:
    def test_coordinates_come_from_the_environment(self, load_handler: Any) -> None:
        module = load_handler(AEM_PROGRAM_ID="p12345", AEM_ENV_ID="e67890", AEM_ENV_TYPE="prod")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess_2026-07-24.log"))
        labels = pushed_streams(opener)[0]["stream"]
        assert labels["aem_program_id"] == "p12345"
        assert labels["aem_env_id"] == "e67890"
        assert labels["aem_env_type"] == "prod"
        assert labels["aem_tier"] == "author"
        assert labels["log_type"] == "aemaccess"
        assert labels["service_name"] == "adobe-aem"

    def test_an_unset_coordinate_omits_its_label(self, load_handler: Any) -> None:
        """Rather than setting it to a placeholder that reads as data."""
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        labels = pushed_streams(opener)[0]["stream"]
        assert "aem_program_id" not in labels
        assert "aem_env_id" not in labels

    def test_a_tier_in_the_key_beats_the_configured_default(self, load_handler: Any) -> None:
        """So one function can serve a bucket holding every tier."""
        module = load_handler(AEM_TIER="author")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="publish_aemaccess.log"))
        assert pushed_streams(opener)[0]["stream"]["aem_tier"] == "publish"

    def test_the_configured_tier_is_used_when_the_key_has_none(self, load_handler: Any) -> None:
        module = load_handler(AEM_TIER="publish")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="opaque-name.log"))
        assert pushed_streams(opener)[0]["stream"]["aem_tier"] == "publish"

    def test_nothing_high_cardinality_reaches_a_label(self, load_handler: Any) -> None:
        """The whole cost argument, asserted at the push boundary."""
        module = load_handler(AEM_ENV_ID="e67890")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        forbidden = {"path", "client_ip", "status", "method", "node_id", "object_key", "request_id"}
        for labels, _line, _metadata in entries(opener):
            assert not forbidden & set(labels), f"high-cardinality label: {labels}"

    def test_level_is_a_label_on_the_error_log(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(
            module,
            "24.07.2026 00:00:00.002 [node] *WARN* [thread] com.example.X something\n"
            "24.07.2026 00:00:00.003 [node] *INFO* [thread] com.example.X other",
        )
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemerror.log"))
        levels = {stream["stream"].get("level") for stream in pushed_streams(opener)}
        assert levels == {"warn", "info"}


class TestMetadata:
    def test_parsed_fields_land_in_structured_metadata(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        _labels, _line, metadata = entries(opener)[1]
        assert metadata["path"] == "/missing.html"
        assert metadata["status"] == "404"
        assert metadata["status_class"] == "4xx"
        assert metadata["client_ip"] == "203.0.113.30"
        assert metadata["object_key"] == "author_aemaccess.log"
        assert metadata["bucket"] == "b"

    def test_detected_level_is_inferred_for_a_log_type_with_no_severity(
        self, load_handler: Any
    ) -> None:
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        found = {metadata.get("detected_level") for _l, _line, metadata in entries(opener)}
        assert found == {"info", "warn"}

    def test_drop_client_ip_removes_it_everywhere(self, load_handler: Any) -> None:
        module = load_handler(DROP_CLIENT_IP="true")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        for _labels, _line, metadata in entries(opener):
            assert "client_ip" not in metadata

    def test_request_correlation_attaches_the_path_to_the_response(self, load_handler: Any) -> None:
        """Including for the response that arrived before its request."""
        module = load_handler()
        opener = wire(module, REQUEST_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemrequest.log"))
        responses = [
            metadata
            for _l, _line, metadata in entries(opener)
            if metadata.get("direction") == "response"
        ]
        paired = [m for m in responses if m.get("correlated") == "true"]
        assert len(paired) == 1
        assert paired[0]["path"] == "/systemready"
        assert paired[0]["duration_ms"] == "42"
        # The 500 has no request line in this object, so it is honestly flagged.
        unpaired = [m for m in responses if m.get("correlated") == "false"]
        assert len(unpaired) == 1
        assert unpaired[0]["status"] == "500"

    def test_correlation_can_be_turned_off(self, load_handler: Any) -> None:
        module = load_handler(CORRELATE_REQUESTS="false")
        opener = wire(module, REQUEST_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemrequest.log"))
        for _labels, _line, metadata in entries(opener):
            assert "correlated" not in metadata

    def test_buffered_entries_keep_their_real_line_number(self, load_handler: Any) -> None:
        """`record` must be the line's actual position, including after a flush.

        The correlator releases a buffered record during a later iteration, so
        reading the loop variable at build time gave the wrong line's number -
        a flushed entry was getting record="0".
        """
        module = load_handler()
        opener = wire(module, REQUEST_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemrequest.log"))
        numbers = sorted(metadata["record"] for _l, _line, metadata in entries(opener))
        assert numbers == ["1", "2", "3"]

    def test_every_line_is_shipped_exactly_once_with_correlation_on(
        self, load_handler: Any
    ) -> None:
        """Buffering must not lose or duplicate a line."""
        module = load_handler()
        opener = wire(module, REQUEST_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemrequest.log"))
        assert len(entries(opener)) == 3


class TestLineContent:
    def test_raw_ships_the_original_line(self, load_handler: Any) -> None:
        module = load_handler(LINE_CONTENT="raw")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        assert entries(opener)[0][1] == ACCESS_LINES.splitlines()[0]

    def test_message_ships_only_the_readable_part(self, load_handler: Any) -> None:
        module = load_handler(LINE_CONTENT="message")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        assert entries(opener)[0][1] == "HEAD /systemready HTTP/1.1"

    def test_raw_survives_the_correlator_buffer(self, load_handler: Any) -> None:
        """The regression: a buffered response outlives the loop that read its line.

        Reaching back for the raw line at build time shipped the parsed fragment
        instead of the original for every deferred response.
        """
        module = load_handler(LINE_CONTENT="raw")
        opener = wire(module, REQUEST_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemrequest.log"))
        lines = {line for _l, line, _m in entries(opener)}
        assert set(REQUEST_LINES.splitlines()) == lines

    def test_an_invalid_line_content_fails_at_import(self, load_handler: Any) -> None:
        from grafana_cloud_common import ConfigError

        with pytest.raises(ConfigError, match="LINE_CONTENT"):
            load_handler(LINE_CONTENT="something")


class TestClassificationFallback:
    def test_content_sniffing_identifies_an_unrecognised_key(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="opaque/2026-07-24T1000.000-abc.log"))
        assert pushed_streams(opener)[0]["stream"]["log_type"] == "aemaccess"

    def test_sniffing_off_leaves_the_log_type_unknown(self, load_handler: Any) -> None:
        """Still shipped, line for line, labelled honestly rather than guessed."""
        module = load_handler(SNIFF_CONTENT="false")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="opaque.log"))
        assert pushed_streams(opener)[0]["stream"]["log_type"] == "unknown"
        assert len(entries(opener)) == 2

    def test_a_custom_key_pattern_supplies_the_environment_coordinates(
        self, load_handler: Any
    ) -> None:
        module = load_handler(
            KEY_PATTERN=(
                r"p(?P<program_id>\d+)/e(?P<env_id>\d+)/(?P<tier>author|publish)/"
                r"(?P<log_type>aem[a-z]+)/"
            )
        )
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="p12345/e67890/publish/aemaccess/x.log"))
        labels = pushed_streams(opener)[0]["stream"]
        assert labels["aem_program_id"] == "12345"
        assert labels["aem_env_id"] == "67890"
        assert labels["aem_tier"] == "publish"

    def test_a_key_pattern_that_captures_nothing_fails_at_import(self, load_handler: Any) -> None:
        from grafana_cloud_common import ConfigError

        with pytest.raises(ConfigError, match="no recognised named group"):
            load_handler(KEY_PATTERN=r"\d+")


class TestObjectHandling:
    def test_a_gzipped_object_is_decompressed(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(module, ACCESS_LINES, gzipped=True)
        shipped = module.handle_object(
            S3ObjectRef(bucket="b", key="author_aemaccess_2026-07-24.log.gz")
        )
        assert shipped == 2
        assert entries(opener)[0][1] == ACCESS_LINES.splitlines()[0]

    def test_an_empty_object_ships_nothing_and_does_not_raise(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(module, "")
        assert module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log")) == 0
        assert opener.recorded == []

    def test_a_key_failing_the_suffix_filter_is_skipped(self, load_handler: Any) -> None:
        module = load_handler(SOURCE_KEY_SUFFIX=".gz")
        opener = wire(module, ACCESS_LINES)
        assert module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log")) == 0
        assert opener.recorded == []

    def test_an_old_timestamp_falls_back_to_ingestion_time(self, load_handler: Any) -> None:
        """Loki 400s a too-old sample and drops the whole batch it travelled in."""
        import time

        module = load_handler(MAX_TIMESTAMP_AGE_SECONDS="60")
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        now_ns = time.time_ns()
        for stream in pushed_streams(opener):
            for entry in stream["values"]:
                # Within a minute of now, not the 2026-07-24 in the line.
                assert abs(now_ns - int(entry[0])) < 60 * 1_000_000_000

    def test_the_event_timestamp_is_used_when_the_fallback_is_disabled(
        self, load_handler: Any
    ) -> None:
        from adobe_aem.parsers import parse_clf_timestamp

        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        module.handle_object(S3ObjectRef(bucket="b", key="author_aemaccess.log"))
        expected = parse_clf_timestamp("24/Jul/2026:00:00:04 +0000")
        assert int(pushed_streams(opener)[0]["values"][0][0]) == expected


class TestLambdaEntryPoint:
    def test_an_sqs_event_ships_the_object_and_reports_no_failures(self, load_handler: Any) -> None:
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        event = {
            "Records": [
                {
                    # Required. Without it the record is not recognised as SQS,
                    # falls through to the direct-S3 path, resolves to no
                    # objects, and the invocation trivially succeeds having done
                    # nothing - which is how this test first passed while
                    # proving nothing.
                    "eventSource": "aws:sqs",
                    "messageId": "m1",
                    "body": json.dumps(
                        {
                            "Records": [
                                {
                                    "eventName": "ObjectCreated:Put",
                                    "s3": {
                                        "bucket": {"name": "b"},
                                        "object": {"key": "author_aemaccess.log"},
                                    },
                                }
                            ]
                        }
                    ),
                }
            ]
        }
        assert module.lambda_handler(event, None) == {"batchItemFailures": []}
        # The assertion that makes the one above mean something.
        assert len(entries(opener)) == 2

    def test_a_delete_notification_is_a_no_op(self, load_handler: Any) -> None:
        """Not a failure: there is no object to fetch and nothing went wrong."""
        module = load_handler()
        opener = wire(module, ACCESS_LINES)
        event = {
            "Records": [
                {
                    "eventName": "ObjectRemoved:Delete",
                    "s3": {
                        "bucket": {"name": "b"},
                        "object": {"key": "author_aemaccess.log"},
                    },
                }
            ]
        }
        assert module.lambda_handler(event, None) == {"batchItemFailures": []}
        assert opener.recorded == []

    def test_a_malformed_sqs_body_is_reported_as_failed(self, load_handler: Any) -> None:
        """So it reaches the DLQ instead of being silently acknowledged."""
        module = load_handler()
        wire(module, ACCESS_LINES)
        event = {"Records": [{"eventSource": "aws:sqs", "messageId": "bad", "body": "not json"}]}
        assert module.lambda_handler(event, None) == {
            "batchItemFailures": [{"itemIdentifier": "bad"}]
        }
