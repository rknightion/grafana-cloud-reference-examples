"""Fakes for the AWS clients the attributor uses."""

from __future__ import annotations

import json
from typing import Any


class FakeDynamoDB:
    """Just enough DynamoDB for AttributionStore: items by pk, the one condition it uses, and
    a BatchGetItem that can hold keys back as UnprocessedKeys."""

    def __init__(self, unprocessed_first: int = 0) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.batch_calls = 0
        self._unprocessed_first = unprocessed_first
        self.updates: list[str] = []

    def get_item(self, **kw: Any) -> dict[str, Any]:
        item = self.items.get(kw["Key"]["pk"]["S"])
        return {"Item": item} if item else {}

    def put_item(self, **kw: Any) -> None:
        item = kw["Item"]
        pk = item["pk"]["S"]
        if "ConditionExpression" in kw:
            existing = self.items.get(pk)
            new_ts = int(kw["ExpressionAttributeValues"][":ts"]["N"])
            if existing is not None and not int(existing["ts"]["N"]) < new_ts:
                err = Exception("condition failed")
                err.response = {"Error": {"Code": "ConditionalCheckFailedException"}}  # type: ignore[attr-defined]
                raise err
        self.items[pk] = item

    def update_item(self, **kw: Any) -> None:
        pk = kw["Key"]["pk"]["S"]
        self.updates.append(pk)
        self.items[pk]["expires_at"] = kw["ExpressionAttributeValues"][":e"]

    def batch_get_item(self, **kw: Any) -> dict[str, Any]:
        self.batch_calls += 1
        ((table, req),) = kw["RequestItems"].items()
        keys = req["Keys"]
        assert len(keys) <= 100, "BatchGetItem accepts at most 100 keys"
        held, served = keys[: self._unprocessed_first], keys[self._unprocessed_first :]
        self._unprocessed_first = 0
        found = [self.items[k["pk"]["S"]] for k in served if k["pk"]["S"] in self.items]
        out: dict[str, Any] = {"Responses": {table: found}}
        if held:
            out["UnprocessedKeys"] = {table: {"Keys": held}}
        return out


class FakeLogs:
    """FilterLogEvents over a fixed list of pages, recording each call's arguments."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(kw)
        index = int(kw.get("nextToken", "0"))
        out: dict[str, Any] = {"events": self.pages[index]}
        if index + 1 < len(self.pages):
            out["nextToken"] = str(index + 1)
        return out


def auth_event(
    stream: str, remote: str, user: str, ts: int, *, success: bool = True
) -> dict[str, Any]:
    """An audit authenticate event in the shape DocumentDB writes it."""
    message = {
        "atype": "authenticate",
        "ts": ts,
        "remote_ip": remote,
        "user": "",
        "param": {"user": user, "mechanism": "SCRAM-SHA-1", "success": success, "error": 0},
    }
    return {"logStreamName": stream, "message": json.dumps(message)}
