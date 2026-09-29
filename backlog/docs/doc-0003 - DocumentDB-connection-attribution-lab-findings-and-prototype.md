---
id: doc-0003
title: 'DocumentDB connection attribution: lab findings and prototype'
type: specification
created_date: '2026-09-29 09:10'
updated_date: '2026-09-29 09:14'
tags:
  - docdb
  - prototype
---
# DocumentDB connection attribution: lab findings and prototype

Source material for the example tracked in the task "DocumentDB connection attribution reference
example". Everything below was run against a live Amazon DocumentDB 5.0.0 cluster (one
db.t3.medium instance, TLS on) with a Grafana Cloud stack as the destination. Identifiers are
replaced with the repository's documented placeholders.

## The question it answers

"Connections to our DocumentDB cluster stay open. Which database user is holding them?"
CloudWatch's `DatabaseConnections` is a cluster or instance total with no user dimension, so it
cannot answer this on its own.

## Verified findings

These were each observed directly, not inferred from documentation. Several contradict what a
reasonable reading of the AWS and MongoDB docs would predict.

1. **An idle DocumentDB connection carries no user.** `$currentOp` with
   `{allUsers: true, idleConnections: true}` returns every open connection, but an idle one only has
   `client` (ip:port), `desc: "Conn"`, `active: false` and `clientMetaData` (driver name and version,
   OS, and `application.name` when the driver sets appName). `effectiveUsers` is present only while
   a connection is running an operation. Grouping by `$effectiveUsers.user` puts every idle
   connection under null. The `currentOp` command form (`{currentOp: 1, $all: 1}`) returns the same
   fields.
2. **The audit log supplies the user.** Each `authenticate` event records `remote_ip` **including
   the client port**, with the user in `param.user`. The top-level `user` field is empty on
   authenticate events. Joining `$currentOp.client` to `authenticate.remote_ip` attributes idle
   connections to users; in the lab, 25 deliberately leaked connections all resolved to the leaking
   user.
3. **`audit_logs=ddl` is enough.** Authenticate events keep arriving after switching the cluster
   parameter from `all` to `ddl`, so DML auditing (the expensive part) is not needed. Switching
   between values is dynamic, but attaching a custom cluster parameter group to a cluster needs an
   instance reboot before any value applies (status `pending-reboot`). Audit events reach CloudWatch
   Logs in `/aws/docdb/<cluster>/audit`, one stream per instance, typically within a minute.
4. **Least privilege for the `$currentOp` aggregation stage is `clusterMonitor` + `read` on
   `admin`.** Tested one at a time, each of `clusterMonitor`, `readAnyDatabase`, `clusterManager`,
   `hostManager`, `dbAdminAnyDatabase`, `clusterAdmin`, `userAdminAnyDatabase`,
   `readWriteAnyDatabase`, and a custom role granting only `inprog` on the cluster, returns
   `Authorization failure` (code 13) for the aggregation stage, with or without `allUsers`.
   `clusterMonitor` alone *can* run the `currentOp` command. `clusterMonitor` + `read`@`admin` runs
   the aggregation and sees every user's connections. Denials show up in the audit log as
   `authCheckFailed`.
5. **`$currentOp` is per instance.** Connect to each instance endpoint with `directConnection=true`
   and aggregate across them; the cluster endpoint only shows the primary.
6. **Unattributed connections are expected.** Every driver opens unauthenticated monitoring
   (heartbeat) connections, one or two per client per instance, which never produce an authenticate
   event. pymongo sets appName on these too, so they appear as `<unattributed>` with the app name;
   the Go driver's appear with an empty app name. Connections opened before the audit lookback
   window, or before auditing was enabled, also cannot be attributed.
7. **The Grafana MongoDB Enterprise data source cannot do this.** It connects to DocumentDB over
   Private Data Source Connect and passes its health check, but its backend accepts only `find` and
   `aggregate` on a collection plus an allowlist of `stats`, `serverStatus`, `replSetGetStatus`,
   `getLog`, `connPoolStats`, `connectionStatus`, `buildInfo`, `dbStats`, `hostInfo`, `lockInfo`.
   `db.aggregate([...])`, `db.currentOp()`, `db.adminCommand(...)` and `db.runCommand(...)` all
   return `bad query`; `admin.<collection>.aggregate([{$currentOp...}])` reaches the server and is
   refused with `$currentOp must be run with {aggregate: 1}`. So a scheduled job has to run the
   join. (Tested with plugin 2.0.1.)
8. **MongoDB data source TLS trap, for the README's troubleshooting section.** `tls=true` in the
   connection string makes the plugin ignore its own CA certificate field, and the health check
   fails with `x509: certificate signed by unknown authority`. Leave `tls=true` out of the URI and set
   `tlsAuthWithCACert` with the RDS CA bundle; TLS still negotiates. Setting `tlsAuth: true` also
   avoids it.

## Sample output

One attributor run with two leaking apps, `app_leaky` (25 connections) and `app_nightly` (15):

```
  25 instance=docdb-lab-1 user=app_leaky app=leaky-batch addr=192.0.2.10
  15 instance=docdb-lab-1 user=app_nightly app=nightly-export addr=192.0.2.10
   3 instance=docdb-lab-1 user=<unattributed> app= addr=192.0.2.20
   1 instance=docdb-lab-1 user=app_reports app=reports-job addr=192.0.2.10
   1 instance=docdb-lab-1 user=app_orders app=orders-service addr=192.0.2.10
   1 instance=docdb-lab-1 user=grafana_ds app=docdb-connection-attributor addr=192.0.2.30
   1 instance=docdb-lab-1 user=<unattributed> app=leaky-batch addr=192.0.2.10
summary {"cluster": "docdb-lab", "connections": 53, "unattributed": 9, "instances": 1}
```

Exported over OTLP, the gauge lands in Mimir as `docdb_connections_open` with labels
`docdb_cluster`, `docdb_instance`, `docdb_user`, `docdb_app_name`, optionally `client_address`,
plus `job` and `service_name` from `service.name`. An empty `docdb_app_name` is dropped as a label
rather than stored empty.

**Put anything you filter on in the data point attributes, not the resource.** The first prototype
set `docdb.cluster` as a resource attribute; the gateway wrote it only onto `target_info`, so the
series carried no cluster label and a dashboard filter `docdb_cluster=~"$cluster"` matched only by
accident (an empty regex matches an absent label).

A raw audit authenticate event:

```json
{"atype": "authenticate", "ts": 1790671928706, "remote_ip": "192.0.2.10:50058", "user": "",
 "param": {"user": "app_leaky", "mechanism": "SCRAM-SHA-1", "success": true, "message": "", "error": 0}}
```

## Prototype source

The lab ran the attributor as a Kubernetes CronJob every minute, with EKS Pod Identity for the AWS
calls and a Grafana Cloud `metrics:write` token. `loadgen.py` creates the lab users and produces the
traffic, including the leak.

### `attributor.py`

```python
"""Attribute open Amazon DocumentDB connections to database users and export them as OTLP metrics.

DocumentDB's $currentOp lists every open connection (idle ones included) with the client ip:port and
the driver's appName, but an idle connection carries no user. The audit log's `authenticate` event
records the same ip:port with the user that authenticated on it. Joining the two gives open
connections per user.

One run takes one snapshot and exits, so it suits a CronJob, an ECS scheduled task or a Lambda.

Environment:
  DOCDB_CLUSTER_ID          cluster identifier; every instance in it is queried
  DOCDB_USERNAME            user with clusterMonitor + read on admin (see README)
  DOCDB_PASSWORD
  DOCDB_CA_FILE             RDS CA bundle, default /app/global-bundle.pem
  AUDIT_LOG_GROUP           default /aws/docdb/<cluster>/audit
  AUDIT_LOOKBACK_MINUTES    how far back to read authenticate events, default 1440
  OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_HEADERS   standard OTel exporter settings
  INCLUDE_CLIENT_ADDRESS    "true" adds client.address (host, no port) as a label; off by default for cardinality
"""

from __future__ import annotations

import collections
import json
import logging
import os
import time

import boto3
from pymongo import MongoClient
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
from opentelemetry.sdk.resources import Resource

log = logging.getLogger("docdb-attributor")
UNATTRIBUTED = "<unattributed>"


def instance_endpoints(docdb, cluster_id: str) -> list[tuple[str, str]]:
    cluster = docdb.describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    out = []
    for m in cluster["DBClusterMembers"]:
        inst = docdb.describe_db_instances(DBInstanceIdentifier=m["DBInstanceIdentifier"])[
            "DBInstances"
        ][0]
        if inst.get("Endpoint"):
            out.append((m["DBInstanceIdentifier"], inst["Endpoint"]["Address"]))
    return out


def open_connections(host: str) -> list[dict]:
    # $currentOp is per instance, so connect directly to the instance endpoint, not the cluster endpoint.
    c = MongoClient(
        host=host,
        port=27017,
        directConnection=True,
        username=os.environ["DOCDB_USERNAME"],
        password=os.environ["DOCDB_PASSWORD"],
        authSource="admin",
        tls=True,
        tlsCAFile=os.environ.get("DOCDB_CA_FILE", "/app/global-bundle.pem"),
        retryWrites=False,
        appName="docdb-connection-attributor",
        serverSelectionTimeoutMS=10000,
    )
    try:
        res = c.admin.command(
            {
                "aggregate": 1,
                "cursor": {},
                "pipeline": [
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
                ],
            }
        )
        return res["cursor"]["firstBatch"]
    finally:
        c.close()


def authenticated_users(logs, group: str, lookback_min: int) -> dict[str, str]:
    """Map client ip:port -> user from successful authenticate events, latest event wins."""
    start = int((time.time() - lookback_min * 60) * 1000)
    who: dict[str, tuple[int, str]] = {}
    pages = logs.get_paginator("filter_log_events").paginate(
        logGroupName=group, startTime=start, filterPattern='{ $.atype = "authenticate" }'
    )
    for page in pages:
        for e in page["events"]:
            m = json.loads(e["message"])
            p = m.get("param") or {}
            if not p.get("success"):
                continue
            # The user is in param.user; the top-level user field is empty on authenticate events.
            prev = who.get(m["remote_ip"])
            if prev is None or m["ts"] > prev[0]:
                who[m["remote_ip"]] = (m["ts"], p["user"])
    return {k: v[1] for k, v in who.items()}


def snapshot() -> tuple[collections.Counter, dict]:
    region = os.environ.get("AWS_REGION", "eu-west-1")
    cluster_id = os.environ["DOCDB_CLUSTER_ID"]
    group = os.environ.get("AUDIT_LOG_GROUP", f"/aws/docdb/{cluster_id}/audit")
    lookback = int(os.environ.get("AUDIT_LOOKBACK_MINUTES", "1440"))
    with_addr = os.environ.get("INCLUDE_CLIENT_ADDRESS", "false").lower() == "true"

    who = authenticated_users(boto3.client("logs", region_name=region), group, lookback)
    counts: collections.Counter = collections.Counter()
    stats = {"connections": 0, "unattributed": 0, "instances": 0}
    for instance_id, host in instance_endpoints(
        boto3.client("docdb", region_name=region), cluster_id
    ):
        stats["instances"] += 1
        for conn in open_connections(host):
            stats["connections"] += 1
            client = conn.get("client", "")
            user = who.get(client)
            if user is None and conn.get("effectiveUsers"):
                user = conn["effectiveUsers"][0]["user"]  # active op carries its own user
            if user is None:
                # Usually a driver's unauthenticated heartbeat connection, or one opened before the lookback.
                user = UNATTRIBUTED
                stats["unattributed"] += 1
            key = (
                instance_id,
                user,
                conn.get("app") or "",
                client.rsplit(":", 1)[0] if with_addr else None,
            )
            counts[key] += 1
    return counts, {"cluster": cluster_id, **stats}


def export(counts: collections.Counter, stats: dict) -> None:
    def observe(_: CallbackOptions):
        for (instance, user, app, addr), n in counts.items():
            attrs = {
                "docdb.cluster": stats["cluster"],
                "docdb.instance": instance,
                "docdb.user": user,
                "docdb.app_name": app,
            }
            if addr is not None:
                attrs["client.address"] = addr
            yield Observation(n, attrs)

    reader = InMemoryMetricReader()
    provider = MeterProvider(
        # Resource attributes land on target_info, not on each series, so the cluster goes on the data point.
        resource=Resource.create({"service.name": "docdb-connection-attributor"}),
        metric_readers=[reader],
    )
    provider.get_meter("docdb-connection-attributor").create_observable_gauge(
        "docdb.connections.open",
        callbacks=[observe],
        unit="{connection}",
        description="Open DocumentDB connections by authenticated user and client app",
    )
    data = reader.get_metrics_data()
    result = OTLPMetricExporter().export(data)
    provider.shutdown()
    if result != MetricExportResult.SUCCESS:
        raise SystemExit(f"OTLP export failed: {result}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    counts, stats = snapshot()
    for (instance, user, app, addr), n in counts.most_common():
        log.info(
            "%4d instance=%s user=%s app=%s%s",
            n,
            instance,
            user,
            app,
            f" addr={addr}" if addr else "",
        )
    log.info("summary %s", json.dumps(stats))
    export(counts, stats)
    log.info("exported %d series", len(counts))


if __name__ == "__main__":
    main()
```

### `requirements.txt`

```text
boto3==1.40.*
pymongo==4.18.*
opentelemetry-sdk==1.37.*
opentelemetry-exporter-otlp-proto-http==1.37.*
```

### `Dockerfile`

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY global-bundle.pem attributor.py ./
USER 65532
ENTRYPOINT ["python", "/app/attributor.py"]
```

### `iam-policy.json`

```json
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":["rds:DescribeDBClusters","rds:DescribeDBInstances"],"Resource":"*"},
 {"Effect":"Allow","Action":["logs:FilterLogEvents"],"Resource":"arn:aws:logs:eu-west-1:123456789012:log-group:/aws/docdb/docdb-lab/audit:*"}]}
```

### `cronjob.yaml`

```yaml
# Lab deployment used to prove the attributor. The IAM role is bound through EKS Pod Identity.
apiVersion: v1
kind: ServiceAccount
metadata: {name: docdb-attributor}
---
apiVersion: batch/v1
kind: CronJob
metadata: {name: docdb-attributor}
spec:
  schedule: "* * * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 50
      template:
        spec:
          serviceAccountName: docdb-attributor
          restartPolicy: Never
          containers:
          - name: attributor
            image: 123456789012.dkr.ecr.eu-west-1.amazonaws.com/docdb-attributor:v1
            env:
            - {name: AWS_REGION, value: eu-west-1}
            - {name: DOCDB_CLUSTER_ID, value: docdb-lab}
            - {name: DOCDB_USERNAME, value: grafana_ds}
            - {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "https://otlp-gateway-prod-eu-west-0.grafana.net/otlp"}
            - {name: INCLUDE_CLIENT_ADDRESS, value: "true"}
            # Secret holds DOCDB_PASSWORD and OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic%20<base64 stackid:token>
            envFrom: [{secretRef: {name: attributor}}]
            securityContext: {allowPrivilegeEscalation: false, runAsNonRoot: true, capabilities: {drop: [ALL]}}
```

### `dashboard.json`

```json
{
  "uid": "docdb-conn-attribution",
  "title": "DocumentDB open connections by user",
  "tags": [
    "docdb",
    "reference-example"
  ],
  "time": {
    "from": "now-30m",
    "to": "now"
  },
  "refresh": "1m",
  "templating": {
    "list": [
      {
        "name": "datasource",
        "type": "datasource",
        "query": "prometheus",
        "current": {
          "text": "grafanacloud-prom",
          "value": "grafanacloud-prom"
        }
      },
      {
        "name": "cluster",
        "type": "query",
        "datasource": {
          "uid": "${datasource}"
        },
        "query": {
          "query": "label_values(docdb_connections_open, docdb_cluster)",
          "refId": "v"
        },
        "refresh": 2,
        "includeAll": true,
        "multi": true
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "stat",
      "title": "Open connections",
      "gridPos": {
        "x": 0,
        "y": 0,
        "w": 6,
        "h": 5
      },
      "datasource": {
        "uid": "${datasource}"
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum(docdb_connections_open{docdb_cluster=~\"$cluster\"})"
        }
      ]
    },
    {
      "id": 2,
      "type": "stat",
      "title": "Unattributed (driver heartbeats, or opened before audit lookback)",
      "gridPos": {
        "x": 6,
        "y": 0,
        "w": 6,
        "h": 5
      },
      "datasource": {
        "uid": "${datasource}"
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum(docdb_connections_open{docdb_cluster=~\"$cluster\",docdb_user=\"<unattributed>\"}) or vector(0)"
        }
      ]
    },
    {
      "id": 3,
      "type": "bargauge",
      "title": "Top users by open connections",
      "gridPos": {
        "x": 12,
        "y": 0,
        "w": 12,
        "h": 5
      },
      "datasource": {
        "uid": "${datasource}"
      },
      "options": {
        "orientation": "horizontal",
        "displayMode": "basic"
      },
      "targets": [
        {
          "refId": "A",
          "expr": "topk(10, sum by (docdb_user) (docdb_connections_open{docdb_cluster=~\"$cluster\",docdb_user!=\"<unattributed>\"}))",
          "instant": true,
          "legendFormat": "{{docdb_user}}"
        }
      ]
    },
    {
      "id": 4,
      "type": "timeseries",
      "title": "Open connections by user",
      "gridPos": {
        "x": 0,
        "y": 5,
        "w": 24,
        "h": 9
      },
      "datasource": {
        "uid": "${datasource}"
      },
      "fieldConfig": {
        "defaults": {
          "custom": {
            "stacking": {
              "mode": "normal"
            },
            "fillOpacity": 30,
            "lineInterpolation": "stepAfter"
          }
        }
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum by (docdb_user) (docdb_connections_open{docdb_cluster=~\"$cluster\"})",
          "legendFormat": "{{docdb_user}}"
        }
      ]
    },
    {
      "id": 5,
      "type": "table",
      "title": "Connections by user, app, client and instance (latest snapshot)",
      "gridPos": {
        "x": 0,
        "y": 14,
        "w": 24,
        "h": 9
      },
      "datasource": {
        "uid": "${datasource}"
      },
      "targets": [
        {
          "refId": "A",
          "expr": "sum by (docdb_cluster, docdb_instance, docdb_user, docdb_app_name, client_address) (docdb_connections_open{docdb_cluster=~\"$cluster\"})",
          "instant": true,
          "format": "table"
        }
      ],
      "transformations": [
        {
          "id": "organize",
          "options": {
            "excludeByName": {
              "Time": true
            },
            "renameByName": {
              "Value": "connections"
            }
          }
        },
        {
          "id": "sortBy",
          "options": {
            "sort": [
              {
                "field": "connections",
                "desc": true
              }
            ]
          }
        }
      ]
    }
  ]
}
```

### `loadgen.py`

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymongo>=4.8"]
# ///
"""DocumentDB connection-attribution lab.

uv run lab.py setup          create lab users and seed data (as the primary user)
uv run lab.py load SECONDS   run three app users against the cluster, one of them holding idle connections
uv run lab.py check USER     run the $currentOp attribution query as USER (labadmin or grafana_monitor)
"""

import os
import sys
import random
import threading
import time
import json

from pymongo import MongoClient

HOST = os.environ["DOCDB_HOST"]
CA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "global-bundle.pem")
PW = {
    "labadmin": os.environ["MASTER_PW"],
    "app_orders": "orders-lab-pw-1",
    "app_reports": "reports-lab-pw-1",
    "app_leaky": "leaky-lab-pw-1",
    "grafana_monitor": "monitor-lab-pw-1",
    "grafana_inprog": "inprog-lab-pw-1",
    "grafana_ds": "grafana-ds-lab-pw-1",
}


def client(user, app, **kw):
    return MongoClient(
        host=HOST,
        port=27017,
        username=user,
        password=PW[user],
        authSource="admin",
        tls=True,
        tlsCAFile=CA,
        replicaSet="rs0",
        readPreference="primary",
        retryWrites=False,
        appName=app,
        serverSelectionTimeoutMS=15000,
        **kw,
    )


def setup():
    c = client("labadmin", "lab-setup")
    admin = c.admin
    users = {
        "app_orders": [{"role": "readWrite", "db": "labdb"}],
        "app_reports": [{"role": "read", "db": "labdb"}],
        "app_leaky": [{"role": "readWrite", "db": "labdb"}],
        "grafana_monitor": [
            {"role": "clusterMonitor", "db": "admin"},
            {"role": "read", "db": "labdb"},
        ],
    }
    existing = {u["user"] for u in admin.command("usersInfo")["users"]}
    for u, roles in users.items():
        if u in existing:
            admin.command("updateUser", u, pwd=PW[u], roles=roles)
        else:
            admin.command("createUser", u, pwd=PW[u], roles=roles)
        print("user", u, roles)
    orders = c.labdb.orders
    if orders.estimated_document_count() < 5000:
        orders.insert_many(
            [
                {
                    "order_id": i,
                    "customer": f"c{random.randint(1, 500)}",
                    "amount": round(random.uniform(5, 500), 2),
                    "status": random.choice(["new", "paid", "shipped"]),
                    "ts": time.time(),
                }
                for i in range(5000)
            ]
        )
    print("orders:", orders.estimated_document_count())


def worker_orders(stop):
    c = client("app_orders", "orders-service", maxPoolSize=5)
    col = c.labdb.orders
    n = 0
    while not stop.is_set():
        col.insert_one(
            {
                "order_id": random.randint(10**6, 10**7),
                "customer": f"c{random.randint(1, 500)}",
                "amount": round(random.uniform(5, 500), 2),
                "status": "new",
                "ts": time.time(),
            }
        )
        col.find_one({"customer": f"c{random.randint(1, 500)}"})
        n += 1
        time.sleep(0.05)
    print("orders-service ops:", n)


def worker_reports(stop):
    c = client("app_reports", "reports-job", maxPoolSize=3)
    col = c.labdb.orders
    while not stop.is_set():
        list(col.aggregate([{"$group": {"_id": "$status", "total": {"$sum": "$amount"}}}]))
        time.sleep(1)


def worker_leaky(stop, n=25):
    # Mimics a batch job that opens a pool and never closes it: n idle authenticated connections held open.
    c = client("app_leaky", "leaky-batch", minPoolSize=n, maxPoolSize=n)
    c.labdb.command("ping")
    stop.wait()


def load(seconds):
    stop = threading.Event()
    ts = [
        threading.Thread(target=f, args=(stop,), daemon=True)
        for f in (worker_orders, worker_reports, worker_leaky)
    ]
    for t in ts:
        t.start()
    print(f"load running for {seconds}s", flush=True)
    time.sleep(seconds)
    stop.set()
    time.sleep(2)


PIPELINE = [
    {"$currentOp": {"allUsers": True, "idleConnections": True}},
    {
        "$group": {
            "_id": {"user": "$effectiveUsers.user", "app": "$clientMetaData.application.name"},
            "count": {"$sum": 1},
        }
    },
    {"$sort": {"count": -1}},
]


def check(user):
    c = client(user, "attribution-check")
    raw = c.admin.command(
        {
            "aggregate": 1,
            "pipeline": [
                {"$currentOp": {"allUsers": True, "idleConnections": True}},
                {"$match": {"desc": "Conn"}},
                {"$limit": 1},
            ],
            "cursor": {},
        }
    )
    print("sample Conn document:")
    print(json.dumps(raw["cursor"]["firstBatch"], default=str, indent=1))
    res = c.admin.command({"aggregate": 1, "pipeline": PIPELINE, "cursor": {}})
    print(f"\nattribution as {user}:")
    for r in res["cursor"]["firstBatch"]:
        print(f"  {r['count']:>4}  user={r['_id'].get('user')}  app={r['_id'].get('app')}")


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "setup":
        setup()
    elif cmd == "load":
        load(int(sys.argv[2]))
    elif cmd == "check":
        check(sys.argv[2])
```
