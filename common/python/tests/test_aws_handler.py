from __future__ import annotations

import json
from typing import Any

import pytest

from grafana_cloud_common.aws.handler import (
    LambdaContext,
    SourceMessage,
    parse_event,
    process_messages,
)
from grafana_cloud_common.aws.s3 import S3ObjectRef
from grafana_cloud_common.errors import PermanentError


def s3_record(key: str, *, bucket: str = "acme-logs", size: int = 42) -> dict[str, Any]:
    return {
        "eventSource": "aws:s3",
        "eventName": "ObjectCreated:Put",
        "s3": {
            "bucket": {"name": bucket},
            "object": {"key": key, "size": size},
        },
    }


def _removal_record(key: str) -> dict[str, Any]:
    record = s3_record(key)
    record["eventName"] = "ObjectRemoved:Delete"
    return record


def sqs_record(message_id: str, body: object) -> dict[str, Any]:
    return {
        "eventSource": "aws:sqs",
        "messageId": message_id,
        "body": body if isinstance(body, str) else json.dumps(body),
    }


class FakeContext:
    aws_request_id = "req-1"
    function_name = "test"
    function_version = "$LATEST"
    memory_limit_in_mb = "512"

    def __init__(self, remaining_ms: int = 300_000) -> None:
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining


class TestParseEvent:
    def test_direct_s3_notification(self) -> None:
        messages = parse_event({"Records": [s3_record("logs/app.log")]})
        assert len(messages) == 1
        assert messages[0].message_id is None
        assert messages[0].objects[0] == S3ObjectRef(
            bucket="acme-logs", key="logs/app.log", size_bytes=42
        )

    def test_url_decodes_the_object_key(self) -> None:
        """S3 encodes the key and uses '+' for spaces. Skipping this produces a
        NoSuchKey that reads like a permissions problem."""
        messages = parse_event({"Records": [s3_record("logs/my+file+%231.log.gz")]})
        assert messages[0].objects[0].key == "logs/my file #1.log.gz"

    def test_sqs_wrapped_s3_notification_keeps_the_message_id(self) -> None:
        event = {"Records": [sqs_record("m-1", {"Records": [s3_record("a.log")]})]}
        messages = parse_event(event)
        assert messages[0].message_id == "m-1"
        assert messages[0].objects[0].key == "a.log"

    def test_ignores_the_s3_test_event(self) -> None:
        """S3 sends this once when a notification config is created. A handler
        that assumes Records exists crashes on the first event after deploy."""
        body = {
            "Service": "Amazon S3",
            "Event": "s3:TestEvent",
            "Bucket": "acme-logs",
        }
        assert parse_event({"Records": [sqs_record("m-1", body)]}) == []

    def test_a_poison_sqs_body_is_reported_as_failed_not_dropped(self) -> None:
        messages = parse_event({"Records": [sqs_record("m-1", "this is not json")]})
        assert messages == [SourceMessage(objects=[], message_id="m-1")]

    def test_eventbridge_object_created(self) -> None:
        event = {
            "detail-type": "Object Created",
            "detail": {
                "bucket": {"name": "acme-logs"},
                "object": {"key": "logs/my file.log", "size": 9},
            },
        }
        messages = parse_event(event)
        # EventBridge does not URL-encode, so the key must pass through untouched.
        assert messages[0].objects[0].key == "logs/my file.log"

    def test_a_removal_notification_yields_no_work(self) -> None:
        """The object is already gone, so acting on it can only produce a
        NoSuchKey and a misleading log line."""
        record = s3_record("gone.log")
        record["eventName"] = "ObjectRemoved:Delete"
        assert parse_event({"Records": [record]}) == []

    def test_an_sqs_wrapped_removal_notification_is_acknowledged_not_failed(self) -> None:
        """Nothing to do is a success. Marking it failed would send every delete
        in the bucket to the dead-letter queue."""
        event = {"Records": [sqs_record("m-1", {"Records": [_removal_record("gone.log")]})]}
        assert parse_event(event) == []

    def test_a_missing_size_on_a_create_event_is_tolerated(self) -> None:
        record = s3_record("a.log")
        del record["s3"]["object"]["size"]
        messages = parse_event({"Records": [record]})
        assert messages[0].objects[0].size_bytes is None

    def test_an_eventbridge_delete_is_ignored(self) -> None:
        event = {
            "detail-type": "Object Deleted",
            "detail": {"bucket": {"name": "acme-logs"}, "object": {"key": "gone.log"}},
        }
        assert parse_event(event) == []

    def test_a_non_string_sqs_body_is_reported_as_failed_not_dropped(self) -> None:
        """A skip here would have Lambda delete the message as handled, so a
        malformed record would vanish without reaching the DLQ."""
        messages = parse_event({"Records": [{"eventSource": "aws:sqs", "messageId": "m-1"}]})
        assert messages == [SourceMessage(objects=[], message_id="m-1")]

    def test_an_sqs_body_that_is_json_but_not_an_object_is_reported_as_failed(self) -> None:
        messages = parse_event({"Records": [sqs_record("m-1", [1, 2, 3])]})
        assert messages == [SourceMessage(objects=[], message_id="m-1")]

    def test_an_unrecognised_event_is_permanent(self) -> None:
        with pytest.raises(PermanentError, match="unrecognised event shape"):
            parse_event({"someOtherTrigger": True})


class TestProcessMessages:
    def test_counts_successes(self) -> None:
        messages = [SourceMessage(objects=[S3ObjectRef("b", "k")], message_id="m-1")]
        outcome = process_messages(messages, lambda _: 7)
        assert outcome.lines_shipped == 7
        assert outcome.objects_processed == 1
        assert outcome.failed_message_ids == []

    def test_one_bad_object_fails_only_its_own_message(self) -> None:
        def handle(ref: S3ObjectRef) -> int:
            if ref.key == "bad":
                raise PermanentError("nope")
            return 1

        messages = [
            SourceMessage(objects=[S3ObjectRef("b", "good")], message_id="m-1"),
            SourceMessage(objects=[S3ObjectRef("b", "bad")], message_id="m-2"),
            SourceMessage(objects=[S3ObjectRef("b", "also-good")], message_id="m-3"),
        ]
        outcome = process_messages(messages, handle)

        assert outcome.failed_message_ids == ["m-2"]
        assert outcome.objects_processed == 2
        assert outcome.objects_failed == 1
        assert outcome.to_response() == {"batchItemFailures": [{"itemIdentifier": "m-2"}]}

    def test_stops_early_and_fails_the_remainder_when_out_of_time(self) -> None:
        """A timeout kill returns no partial-failure response at all, so the whole
        batch redelivers and every shipped line is duplicated."""
        messages = [
            SourceMessage(objects=[S3ObjectRef("b", f"k{index}")], message_id=f"m-{index}")
            for index in range(3)
        ]
        context: LambdaContext = FakeContext(remaining_ms=1_000)
        outcome = process_messages(messages, lambda _: 1, context=context, time_budget_ms=15_000)

        assert outcome.objects_processed == 0
        assert outcome.failed_message_ids == ["m-0", "m-1", "m-2"]

    def test_a_message_with_an_id_and_no_objects_is_failed(self) -> None:
        """This is how parse_event reports a malformed SQS record: nothing to
        process, and it must not be acknowledged."""
        outcome = process_messages([SourceMessage(objects=[], message_id="m-1")], lambda _: 0)
        assert outcome.failed_message_ids == ["m-1"]
        assert outcome.objects_processed == 0

    def test_a_message_with_no_id_and_no_objects_is_a_no_op(self) -> None:
        outcome = process_messages([SourceMessage(objects=[])], lambda _: 0)
        assert outcome.failed_message_ids == []

    def test_proceeds_when_there_is_time(self) -> None:
        messages = [SourceMessage(objects=[S3ObjectRef("b", "k")], message_id="m-1")]
        context: LambdaContext = FakeContext(remaining_ms=300_000)
        outcome = process_messages(messages, lambda _: 3, context=context)
        assert outcome.lines_shipped == 3
        assert outcome.failed_message_ids == []
