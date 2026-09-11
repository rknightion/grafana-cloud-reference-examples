"""Lambda event plumbing: parse the event, report partial failures, keep state warm.

Three event shapes reach an S3-to-Loki function and all three are normal:

- a direct S3 bucket notification
- an S3 notification delivered through SQS, which is what you want at any real
  volume because it gives you batching, a retry policy and a DLQ
- an EventBridge ``Object Created`` event, if the bucket already routes to it

They are normalised to the same ``S3ObjectRef`` list so a handler has one path.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote_plus

from grafana_cloud_common.errors import PermanentError
from grafana_cloud_common.log import get_logger

from .s3 import S3ObjectRef

_LOG = get_logger(__name__)

# S3 sends this once when a notification configuration is created. It has no
# Records key, and a handler that assumes one crashes on the very first event
# after deployment, which reads like a broken function.
_S3_TEST_EVENT_SERVICE = "Amazon S3"

# A removal notification names an object that is already gone, so acting on it
# means a GetObject that can only ever return NoSuchKey. That is classified
# permanent, so it does not loop, but it still costs an invocation and puts a
# misleading error in the log. Filtered here instead.
_IGNORED_EVENT_PREFIXES = ("ObjectRemoved",)


class LambdaContext(Protocol):
    """The subset of the Lambda context object worth depending on.

    A Protocol rather than an import, so nothing here needs the `aws-lambda-typing`
    package and the deployment package stays dependency-free.
    """

    aws_request_id: str
    function_name: str
    function_version: str
    memory_limit_in_mb: str

    def get_remaining_time_in_millis(self) -> int: ...


@dataclass(slots=True)
class SourceMessage:
    """One unit of work, and the SQS receipt that has to be reported on failure.

    ``message_id`` is None for a direct S3 or EventBridge invocation, where the
    whole invocation succeeds or fails together and there is nothing partial to
    report.
    """

    objects: list[S3ObjectRef]
    message_id: str | None = None


@dataclass(slots=True)
class BatchOutcome:
    """What to hand back to Lambda, plus counters worth logging."""

    failed_message_ids: list[str] = field(default_factory=list)
    objects_processed: int = 0
    objects_failed: int = 0
    lines_shipped: int = 0

    def to_response(self) -> dict[str, Any]:
        """Build the SQS partial-batch-failure response.

        This only takes effect when the event source mapping has
        ``FunctionResponseTypes = ["ReportBatchItemFailures"]``. Without it Lambda
        ignores the response and redelivers the whole batch, which silently
        duplicates every successfully shipped line. The shared Terraform module
        and the CloudFormation template both set it.
        """
        return {"batchItemFailures": [{"itemIdentifier": mid} for mid in self.failed_message_ids]}


def _object_from_s3_record(record: dict[str, Any]) -> S3ObjectRef | None:
    event_name = str(record.get("eventName", ""))
    if event_name.startswith(_IGNORED_EVENT_PREFIXES):
        return None

    s3_block = record.get("s3")
    if not isinstance(s3_block, dict):
        return None
    bucket = s3_block.get("bucket", {}).get("name")
    obj = s3_block.get("object", {})
    key = obj.get("key")
    if not bucket or not key:
        return None

    return S3ObjectRef(
        bucket=str(bucket),
        # S3 URL-encodes the key and encodes spaces as '+'. A handler that skips
        # this fails on any object whose name contains a space, a '#' or a '+',
        # with a NoSuchKey that looks like a permissions problem.
        key=unquote_plus(str(key)),
        size_bytes=int(obj["size"]) if isinstance(obj.get("size"), int) else None,
        version_id=str(obj["versionId"]) if obj.get("versionId") else None,
    )


def _objects_from_eventbridge(detail: dict[str, Any]) -> list[S3ObjectRef]:
    bucket = detail.get("bucket", {}).get("name")
    obj = detail.get("object", {})
    key = obj.get("key")
    if not bucket or not key:
        return []
    return [
        S3ObjectRef(
            bucket=str(bucket),
            # EventBridge does NOT URL-encode the key, unlike an S3 notification.
            key=str(key),
            size_bytes=int(obj["size"]) if isinstance(obj.get("size"), int) else None,
            version_id=str(obj["version-id"]) if obj.get("version-id") else None,
        )
    ]


def parse_event(event: dict[str, Any]) -> list[SourceMessage]:
    """Normalise any supported event into units of work.

    Returns an empty list for an event that carries no objects to process - an
    S3 test event, or a removal notification. That is a success, not an error.

    A malformed SQS record is different: it comes back as a message with no
    objects and its message id set, so process_messages marks it failed and it
    reaches the dead-letter queue instead of being silently acknowledged.
    """
    # Any EventBridge S3 event is recognised, so a delete is ignored rather than
    # reported as an unknown shape - but only a creation yields work. See
    # _IGNORED_EVENT_PREFIXES.
    detail_type = event.get("detail-type")
    if isinstance(detail_type, str) and detail_type.startswith("Object "):
        if detail_type != "Object Created":
            _LOG.debug("ignoring eventbridge event", detail_type=detail_type)
            return []
        detail = event.get("detail")
        objects = _objects_from_eventbridge(detail) if isinstance(detail, dict) else []
        return [SourceMessage(objects=objects)] if objects else []

    records = event.get("Records")
    if not isinstance(records, list):
        raise PermanentError(
            f"unrecognised event shape; keys were {sorted(event)}. Expected an S3 "
            f"notification, an SQS batch, or an EventBridge S3 event."
        )

    messages: list[SourceMessage] = []
    direct_objects: list[S3ObjectRef] = []

    for record in records:
        if not isinstance(record, dict):
            continue

        if record.get("eventSource") == "aws:sqs":
            message_id = str(record.get("messageId", ""))
            body = record.get("body")
            if not isinstance(body, str):
                # Reported as failed rather than skipped. A `continue` here
                # would have Lambda delete the message as handled, so a
                # malformed record would vanish without ever reaching the DLQ.
                _LOG.error("sqs record has no string body", exc_info=False, message_id=message_id)
                messages.append(SourceMessage(objects=[], message_id=message_id))
                continue
            try:
                inner = json.loads(body)
            except json.JSONDecodeError as exc:
                # A poison message. Reported as failed so it reaches the DLQ
                # rather than being silently dropped.
                _LOG.error(
                    "sqs body is not JSON", exc_info=False, message_id=message_id, cause=str(exc)
                )
                messages.append(SourceMessage(objects=[], message_id=message_id))
                continue
            if not isinstance(inner, dict):
                _LOG.error("sqs body is not a JSON object", exc_info=False, message_id=message_id)
                messages.append(SourceMessage(objects=[], message_id=message_id))
                continue
            if inner.get("Service") == _S3_TEST_EVENT_SERVICE and "Event" in inner:
                _LOG.info("ignoring s3 test event", message_id=message_id)
                continue
            inner_records = inner.get("Records")
            objects = (
                [
                    ref
                    for raw in inner_records
                    if isinstance(raw, dict) and (ref := _object_from_s3_record(raw)) is not None
                ]
                if isinstance(inner_records, list)
                else []
            )
            # A message that resolves to no objects - a removal notification, or
            # a shape carrying no s3 block - is a success with nothing to do, so
            # it is acknowledged rather than failed.
            if objects:
                messages.append(SourceMessage(objects=objects, message_id=message_id))
            continue

        ref = _object_from_s3_record(record)
        if ref is not None:
            direct_objects.append(ref)

    if direct_objects:
        messages.append(SourceMessage(objects=direct_objects))
    return messages


def process_messages(
    messages: Iterable[SourceMessage],
    handle_object: Callable[[S3ObjectRef], int],
    *,
    context: LambdaContext | None = None,
    time_budget_ms: int = 15_000,
) -> BatchOutcome:
    """Run ``handle_object`` over every object, isolating failures per message.

    A PermanentError on one object is logged and that message is marked failed;
    every other message in the batch still ships. A RetryableError is also marked
    failed, so SQS redelivers just that message.

    Stops early and marks the remainder failed when less than ``time_budget_ms``
    of the invocation remains, rather than being killed mid-push. A timeout kill
    gives no partial-failure response at all, so the entire batch redelivers and
    every line already shipped is duplicated.
    """
    outcome = BatchOutcome()
    out_of_time = False

    for message in messages:
        if out_of_time:
            if message.message_id:
                outcome.failed_message_ids.append(message.message_id)
            continue

        if context is not None and context.get_remaining_time_in_millis() < time_budget_ms:
            _LOG.warning(
                "stopping early to preserve the partial-failure response",
                remaining_ms=context.get_remaining_time_in_millis(),
                time_budget_ms=time_budget_ms,
            )
            out_of_time = True
            if message.message_id:
                outcome.failed_message_ids.append(message.message_id)
            continue

        # parse_event represents a malformed SQS record as "has an id, has no
        # objects". Nothing to process, and it must not be acknowledged.
        if message.message_id and not message.objects:
            outcome.failed_message_ids.append(message.message_id)
            continue

        failed = False
        for ref in message.objects:
            try:
                outcome.lines_shipped += handle_object(ref)
                outcome.objects_processed += 1
            # Deliberately broad: one bad object must not fail the whole batch.
            except Exception as exc:
                failed = True
                outcome.objects_failed += 1
                _LOG.error("object failed", uri=ref.uri, error_type=type(exc).__name__)
        if failed and message.message_id:
            outcome.failed_message_ids.append(message.message_id)

    return outcome
