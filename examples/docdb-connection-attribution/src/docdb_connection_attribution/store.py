"""DynamoDB state: which user authenticated on each (instance, ip:port), plus the audit checkpoint.

One item per client socket, keyed ``conn#<instance>#<ip:port>``, and one checkpoint item. The
mapping has to outlive a single run because a leaked connection can stay open for days after
the only audit event that names its user.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

CHECKPOINT_KEY = "checkpoint"
_BATCH_GET_LIMIT = 100


class DynamoDBSender(Protocol):
    def get_item(self, **kwargs: Any) -> Any: ...
    def put_item(self, **kwargs: Any) -> Any: ...
    def update_item(self, **kwargs: Any) -> Any: ...
    def batch_get_item(self, **kwargs: Any) -> Any: ...


def mapping_key(instance: str, client: str) -> str:
    return f"conn#{instance}#{client}"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """Where the next audit read starts.

    ``resume_*`` is set only when a run ran out of time mid-window: the next run continues that
    exact window from its page token rather than starting a new one and skipping the rest.
    """

    next_start_ms: int | None = None
    resume_start_ms: int | None = None
    resume_end_ms: int | None = None
    resume_token: str | None = None


class AttributionStore:
    __slots__ = ("_client", "_table", "_ttl_seconds")

    def __init__(self, client: DynamoDBSender, table: str, ttl_days: int) -> None:
        self._client = client
        self._table = table
        self._ttl_seconds = ttl_days * 86400

    def load_checkpoint(self) -> Checkpoint:
        item = self._client.get_item(
            TableName=self._table, Key={"pk": {"S": CHECKPOINT_KEY}}, ConsistentRead=True
        ).get("Item")
        if not item:
            return Checkpoint()

        def num(name: str) -> int | None:
            return int(item[name]["N"]) if name in item else None

        token = item.get("resume_token", {}).get("S")
        return Checkpoint(num("next_start_ms"), num("resume_start_ms"), num("resume_end_ms"), token)

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        item: dict[str, Any] = {"pk": {"S": CHECKPOINT_KEY}}
        for name in ("next_start_ms", "resume_start_ms", "resume_end_ms"):
            value = getattr(checkpoint, name)
            if value is not None:
                item[name] = {"N": str(value)}
        if checkpoint.resume_token:
            item["resume_token"] = {"S": checkpoint.resume_token}
        self._client.put_item(TableName=self._table, Item=item)

    def record(self, instance: str, client: str, user: str, ts_ms: int) -> None:
        """Store who authenticated on this socket, unless a newer event is already stored.

        The audit window overlaps between runs on purpose, so the same event arrives more than
        once; the condition makes that idempotent and keeps out-of-order delivery from
        overwriting a newer user with an older one.
        """
        expires_at = int(time.time()) + self._ttl_seconds
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    "pk": {"S": mapping_key(instance, client)},
                    "user": {"S": user},
                    "ts": {"N": str(ts_ms)},
                    "expires_at": {"N": str(expires_at)},
                },
                ConditionExpression="attribute_not_exists(pk) OR ts < :ts",
                ExpressionAttributeValues={":ts": {"N": str(ts_ms)}},
            )
        except Exception as exc:  # botocore's modelled exceptions are per-client classes
            if _error_code(exc) != "ConditionalCheckFailedException":
                raise

    def lookup(self, keys: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Users for these (instance, ip:port) sockets. Refreshes the TTL of each hit, so a
        connection that stays open keeps its attribution beyond the TTL."""
        wanted = {mapping_key(i, c): (i, c) for i, c in keys}
        found: dict[tuple[str, str], str] = {}
        stale: list[str] = []
        refresh_before = int(time.time()) + self._ttl_seconds // 2
        pending = list(wanted)
        while pending:
            chunk, pending = pending[:_BATCH_GET_LIMIT], pending[_BATCH_GET_LIMIT:]
            request: Mapping[str, Any] = {self._table: {"Keys": [{"pk": {"S": k}} for k in chunk]}}
            while request:
                resp = self._client.batch_get_item(RequestItems=request)
                for item in resp.get("Responses", {}).get(self._table, []):
                    pk = item["pk"]["S"]
                    found[wanted[pk]] = item["user"]["S"]
                    if int(item.get("expires_at", {}).get("N", "0")) < refresh_before:
                        stale.append(pk)
                request = resp.get("UnprocessedKeys") or {}
        expires_at = str(int(time.time()) + self._ttl_seconds)
        for pk in stale:
            self._client.update_item(
                TableName=self._table,
                Key={"pk": {"S": pk}},
                UpdateExpression="SET expires_at = :e",
                ExpressionAttributeValues={":e": {"N": expires_at}},
            )
        return found


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""
