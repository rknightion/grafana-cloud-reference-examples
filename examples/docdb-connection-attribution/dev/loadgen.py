# /// script
# requires-python = ">=3.12"
# dependencies = ["pymongo>=4.15,<5"]
# ///
"""Lab traffic for trying the example against a test DocumentDB cluster. Never run it against
production: it creates users and holds connections open on purpose.

Run it from somewhere that can reach the cluster (same VPC, or a VPN into it):

  export DOCDB_HOST=<cluster endpoint>            # the cluster endpoint, not an instance
  export DOCDB_ADMIN_USER=<primary user> DOCDB_ADMIN_PASSWORD=<its password>
  export LAB_USER_PASSWORD=<a throwaway password for the lab users>
  export DOCDB_CA_FILE=global-bundle.pem          # the RDS CA bundle, downloaded beforehand

  uv run dev/loadgen.py setup                     # create lab users, seed a collection
  uv run dev/loadgen.py load 600                  # 10 minutes: busy apps, a slow report, a leak
  uv run dev/loadgen.py monitor-user <name>       # optional: least-privilege attributor user
"""

from __future__ import annotations

import os
import random
import sys
import threading
import time
from typing import Any

from pymongo import MongoClient

LAB_USERS: dict[str, list[dict[str, str]]] = {
    "app_orders": [{"role": "readWrite", "db": "labdb"}],
    "app_reports": [{"role": "read", "db": "labdb"}, {"role": "read", "db": "analytics"}],
    "app_leaky": [{"role": "readWrite", "db": "labdb"}],
}
# A custom role carrying only viewUser, so the function can read every user's roles without
# being able to change any of them.
VIEW_USERS_ROLE = "viewUsersOnly"
VIEW_USERS_PRIVILEGES = [{"resource": {"db": "admin", "collection": ""}, "actions": ["viewUser"]}]
# The least privilege that can run the $currentOp aggregation stage for every user, plus the
# role lookup.
MONITOR_ROLES = [
    {"role": "clusterMonitor", "db": "admin"},
    {"role": "read", "db": "admin"},
    {"role": VIEW_USERS_ROLE, "db": "admin"},
]


def _client(user: str, password: str, app: str, **kw: Any) -> MongoClient[dict[str, Any]]:
    return MongoClient(
        host=os.environ["DOCDB_HOST"],
        port=27017,
        username=user,
        password=password,
        authSource="admin",
        tls=True,
        tlsCAFile=os.environ.get("DOCDB_CA_FILE", "global-bundle.pem"),
        replicaSet="rs0",
        readPreference="primary",
        retryWrites=False,
        appName=app,
        serverSelectionTimeoutMS=15_000,
        **kw,
    )


def _admin() -> MongoClient[dict[str, Any]]:
    return _client(os.environ["DOCDB_ADMIN_USER"], os.environ["DOCDB_ADMIN_PASSWORD"], "lab-setup")


def _upsert_user(admin: Any, name: str, password: str, roles: list[dict[str, str]]) -> None:
    existing = {u["user"] for u in admin.command("usersInfo")["users"]}
    verb = "updateUser" if name in existing else "createUser"
    admin.command(verb, name, pwd=password, roles=roles)
    print(f"{verb} {name}")


def setup() -> None:
    client = _admin()
    for name, roles in LAB_USERS.items():
        _upsert_user(client.admin, name, os.environ["LAB_USER_PASSWORD"], roles)
    orders = client.labdb.orders
    if orders.estimated_document_count() < 1000:
        orders.insert_many({"order_id": i, "customer": f"c{i % 97}"} for i in range(1000))
    # Big enough that a full $group takes long enough to be caught running by a snapshot.
    events = client.labdb.events
    if events.estimated_document_count() < 200_000:
        events.insert_many({"i": i, "c": i % 97, "pad": "x" * 200} for i in range(200_000))


def monitor_user(name: str) -> None:
    password = os.environ.get("MONITOR_USER_PASSWORD")
    if not password:
        sys.exit("set MONITOR_USER_PASSWORD, then store the same value in the attributor's secret")
    admin = _admin().admin
    existing = {r["role"] for r in admin.command("rolesInfo", 1)["roles"]}
    verb = "updateRole" if VIEW_USERS_ROLE in existing else "createRole"
    admin.command(verb, VIEW_USERS_ROLE, privileges=VIEW_USERS_PRIVILEGES, roles=[])
    print(f"{verb} {VIEW_USERS_ROLE}")
    _upsert_user(admin, name, password, MONITOR_ROLES)


def _busy(user: str, app: str, stop: threading.Event, pause_s: float) -> None:
    client = _client(user, os.environ["LAB_USER_PASSWORD"], app, maxPoolSize=5)
    orders = client.labdb.orders
    while not stop.is_set():
        orders.find_one({"customer": f"c{random.randint(0, 96)}"})
        stop.wait(pause_s)


def _slow_report(stop: threading.Event) -> None:
    # Long-running scans, so some connections are active when a snapshot lands.
    client = _client("app_reports", os.environ["LAB_USER_PASSWORD"], "reports-scan", maxPoolSize=3)
    events = client.labdb.events
    pipeline = [{"$group": {"_id": "$c", "n": {"$sum": 1}, "len": {"$sum": {"$strLenCP": "$pad"}}}}]
    while not stop.is_set():
        list(events.aggregate(pipeline))


def _leak(stop: threading.Event, connections: int = 25) -> None:
    # A batch job that opens a pool and never closes it: idle authenticated connections held.
    client = _client(
        "app_leaky",
        os.environ["LAB_USER_PASSWORD"],
        "leaky-batch",
        minPoolSize=connections,
        maxPoolSize=connections,
    )
    client.labdb.command("ping")
    stop.wait()


def load(seconds: int) -> None:
    stop = threading.Event()
    workers = [
        threading.Thread(target=_busy, args=("app_orders", "orders-service", stop, 0.05)),
        threading.Thread(target=_busy, args=("app_reports", "reports-job", stop, 1.0)),
        threading.Thread(target=_slow_report, args=(stop,)),
        threading.Thread(target=_slow_report, args=(stop,)),
        threading.Thread(target=_leak, args=(stop,)),
    ]
    for w in workers:
        w.daemon = True
        w.start()
    print(f"running for {seconds}s")
    time.sleep(seconds)
    stop.set()


if __name__ == "__main__":
    match sys.argv[1:]:
        case ["setup"]:
            setup()
        case ["load", seconds]:
            load(int(seconds))
        case ["monitor-user", name]:
            monitor_user(name)
        case _:
            sys.exit(__doc__)
