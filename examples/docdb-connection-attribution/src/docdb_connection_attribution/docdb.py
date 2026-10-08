"""DocumentDB access: find the instances, then list each one's open connections."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.request
from typing import Any, Protocol

from pymongo import MongoClient
from pymongo.errors import OperationFailure

from grafana_cloud_common import ConfigError

# $currentOp is refused (code 13, Authorization failure) unless the user has clusterMonitor AND
# read on admin; clusterMonitor alone can run only the currentOp command form.
CURRENT_OP_PIPELINE: list[dict[str, Any]] = [
    {"$currentOp": {"allUsers": True, "idleConnections": True}},
    {"$match": {"desc": "Conn"}},
    {
        "$project": {
            "_id": 0,
            "client": 1,
            "active": 1,
            "effectiveUsers": 1,
            "app": "$clientMetaData.application.name",
        }
    },
]


UNAUTHORIZED = 13


class RdsSender(Protocol):
    def describe_db_clusters(self, **kwargs: Any) -> Any: ...
    def describe_db_instances(self, **kwargs: Any) -> Any: ...


class SecretsSender(Protocol):
    def get_secret_value(self, **kwargs: Any) -> Any: ...


def instance_endpoints(rds: RdsSender, cluster_id: str) -> list[tuple[str, str]]:
    """(instance id, endpoint address) for every available instance in the cluster.

    $currentOp only reports the instance it runs on, so each one is queried directly; the
    cluster endpoint would show the primary alone.
    """
    clusters = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"]
    if not clusters:
        raise ConfigError(f"DocumentDB cluster {cluster_id!r} not found")
    out: list[tuple[str, str]] = []
    for member in clusters[0].get("DBClusterMembers", []):
        instance_id = member["DBInstanceIdentifier"]
        inst = rds.describe_db_instances(DBInstanceIdentifier=instance_id)["DBInstances"][0]
        endpoint = inst.get("Endpoint") or {}
        if inst.get("DBInstanceStatus") == "available" and endpoint.get("Address"):
            out.append((instance_id, str(endpoint["Address"])))
    return out


def docdb_credentials(secrets: SecretsSender, secret_id: str) -> tuple[str, str]:
    """(username, password) from a secret in the shape DocumentDB's own managed secrets use."""
    raw = secrets.get_secret_value(SecretId=secret_id)["SecretString"]
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "DOCDB_CREDENTIALS_SECRET_ID must be JSON with username and password"
        ) from exc
    if not isinstance(doc, dict) or not doc.get("username") or not doc.get("password"):
        raise ConfigError("DOCDB_CREDENTIALS_SECRET_ID must be JSON with username and password")
    return str(doc["username"]), str(doc["password"])


def fetch_ca_bundle(url: str) -> str:
    """Download the RDS CA bundle once per execution environment and return its path."""
    path = os.path.join(tempfile.gettempdir(), "rds-ca-bundle.pem")
    if not os.path.exists(path):
        if not url.startswith("https://"):
            raise ConfigError("DOCDB_CA_BUNDLE_URL must be an https:// URL")
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - https enforced above
            body = resp.read()
        if b"BEGIN CERTIFICATE" not in body:
            raise ConfigError(f"{url} did not return a PEM certificate bundle")
        with open(path, "wb") as fh:
            fh.write(body)
    return path


class ConnectionLister:
    """Caches one MongoClient per instance across invocations.

    Reusing the client matters beyond latency: every fresh connection is itself an
    authenticate event, so reconnecting each minute would add audit volume for nothing.
    """

    __slots__ = ("_ca_path", "_clients", "_password", "_username")

    def __init__(self, username: str, password: str, ca_path: str) -> None:
        self._username = username
        self._password = password
        self._ca_path = ca_path
        self._clients: dict[str, MongoClient[dict[str, Any]]] = {}

    def _client(self, host: str) -> MongoClient[dict[str, Any]]:
        client = self._clients.get(host)
        if client is None:
            client = MongoClient(
                host=host,
                port=27017,
                directConnection=True,
                username=self._username,
                password=self._password,
                authSource="admin",
                tls=True,
                tlsCAFile=self._ca_path,
                retryWrites=False,
                appName="docdb-connection-attribution",
                # Bounded so a stalled network read fails inside the Lambda deadline instead
                # of hanging the run until it is killed with no log line.
                serverSelectionTimeoutMS=10_000,
                connectTimeoutMS=5_000,
                socketTimeoutMS=10_000,
            )
            self._clients[host] = client
        return client

    def open_connections(self, host: str) -> list[dict[str, Any]]:
        # A database-level aggregate; iterating the cursor follows getMore, so a cluster with
        # more connections than one batch (101 documents) is not silently truncated.
        return list(self._client(host).admin.aggregate(CURRENT_OP_PIPELINE))

    def users(self, host: str) -> list[dict[str, Any]] | None:
        """Every user and its granted roles, or None when this user may not view other users.

        Roles belong to the user, not the connection, so this is cluster-wide and read once a
        run. It needs the viewUser action on admin, which clusterMonitor does not grant; without
        it the connection counts still export and only the role series are missing.
        """
        try:
            return list(self._client(host).admin.command("usersInfo", 1).get("users", []))
        except OperationFailure as exc:
            if exc.code == UNAUTHORIZED:
                return None
            raise

    def forget(self, host: str) -> None:
        client = self._clients.pop(host, None)
        if client is not None:
            client.close()
