---
id: GRE-0001
title: DocumentDB connection attribution reference example
status: Done
assignee: []
created_date: '2026-09-29 09:12'
updated_date: '2026-09-29 11:26'
labels: []
dependencies: []
references:
  - >-
    https://docs.aws.amazon.com/documentdb/latest/developerguide/event-auditing.html
  - >-
    https://docs.aws.amazon.com/documentdb/latest/developerguide/user_diagnostics.html
  - 'https://grafana.com/docs/plugins/grafana-mongodb-datasource/latest/'
documentation:
  - >-
    backlog/docs/doc-0003 -
    DocumentDB-connection-attribution-lab-findings-and-prototype.md
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Customers running Amazon DocumentDB ask which database user is holding connections open. CloudWatch's DatabaseConnections has no user dimension, the obvious $currentOp query returns no user for idle connections, and the Grafana MongoDB Enterprise data source cannot run $currentOp at all, so there is no Grafana-native answer today. A lab proved a working method: join $currentOp's open connections (client ip:port, appName) to the audit log's authenticate events (remote_ip ip:port -> param.user), then export a gauge per user and app over OTLP. This example packages that as something a customer can deploy.

Everything verified in the lab, the traps, sample output and the complete working prototype source (attributor, Dockerfile, IAM policy, CronJob, dashboard JSON and the load generator that produces a leak) are in doc-0003. Read it before starting: several findings contradict what the AWS and MongoDB docs suggest, and the least-privilege role, the audit_logs=ddl finding and the per-instance requirement all came from testing, not documentation.

Open design question for the worker: the prototype ran as a Kubernetes CronJob, but this repo's deliverables are Lambda zips with Terraform and CloudFormation. A VPC-attached Lambda on an EventBridge schedule fits the repo; it needs egress to the Grafana Cloud OTLP gateway and to the RDS and CloudWatch Logs APIs (NAT or VPC endpoints). Settle the deliverable shape before building. The repo's OTLP guidance (docs/otlp-vs-loki-push.md) is about logs; this example exports metrics, where OTLP is the right fit.

The lab captured dashboard and raw-metric screenshots, but they are not in this repo: the release scan forbids binary files and the lab images show private client IPs.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 examples/docdb-connection-attribution exists with the full required file set, a status other than planned, and passes just check and just package
- [x] #2 The attributor attributes idle connections by joining $currentOp client ip:port to audit authenticate remote_ip, queries every instance endpoint with directConnection, and exports docdb.connections.open with cluster, instance, user and app as data point attributes (not resource attributes)
- [x] #3 Unit tests cover the join: latest authenticate event wins for a reused ip:port, failed authentications are ignored, an active connection falls back to effectiveUsers, and an unmatched connection is counted as <unattributed>
- [x] #4 The README states the least-privilege DocumentDB role (clusterMonitor plus read on admin), that audit_logs=ddl is sufficient, that attaching a custom parameter group needs a reboot, and why unattributed connections exist
- [x] #5 The README explains why the MongoDB Enterprise data source cannot do this, and documents its tls=true connection-string trap for customers who use that data source for other queries
- [x] #6 A dashboard ships with the example and works with the cluster filter, verified against real exported data rather than only rendered
- [x] #7 Client address is opt-in and off by default, and the README names its cardinality cost
- [x] #8 A decision is recorded on whether and how screenshots ship (sanitised, with no private IPs), and if they ship they pass the public-release scan
- [x] #9 Dashboard queries use a lookback window tied to the schedule (last_over_time over at least two intervals), and the README explains why
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [x] #1 just check
- [x] #2 just package-all
<!-- DOD:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
The prototype now lives in examples/docdb-connection-attribution/ (status: planned): prototype/ holds attributor.py, requirements.txt, Dockerfile, iam-policy.json, cronjob.yaml, dashboard.json and loadgen.py, and screenshots/ holds sanitised dashboard and raw-series captures (client address label off, no private IPs). The root README table lists it as planned. mypy excludes examples/*/prototype/ because that code is unpackaged lab source with uninstalled third-party imports; drop that exclude once the real src/ exists. The RDS CA bundle is not committed (the release scan forbids .pem): the Dockerfile ADDs it from the AWS truststore at build time. release-please has no entry for this component yet; add it when the example stops being planned.

Dashboard staleness, found while capturing screenshots: each run pushes one OTLP snapshot and nothing marks a series stale when its connections close, so a plain docdb_connections_open query kept showing a released 15-connection leak for about 5 more minutes (the Prometheus lookback). The prototype dashboard now wraps every query in last_over_time(...[2m]), verified to drop the series within two runs. The shipped README should state the schedule-to-window relationship (window at least 2x the schedule interval).

Review fixes applied to the prototype after the lab was torn down, so they are verified offline against fakes, not re-run on a live cluster: $currentOp now iterates the full cursor (the first version read only firstBatch, which truncates at 101 connections); audit matches are keyed by (log stream = instance id, ip:port) so the same ip:port on two instances cannot collide; an active connection's effectiveUsers wins over a possibly stale audit match. Re-run against a live cluster when building the example.

Open design decision from review: every run re-reads authenticate events for the whole AUDIT_LOOKBACK_MINUTES window (default 24h) with FilterLogEvents. Fine for the lab; on a busy cluster that is a per-minute rescan with CloudWatch Logs cost. Options for the worker: incremental checkpoint with durable state (for example a small DynamoDB table or S3 object holding ip:port -> user and a last-read timestamp), a shorter window plus a documented attribution gap for long-lived connections, or CloudWatch Logs Insights. Decide and document the trade-off in the README.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Shipped examples/docdb-connection-attribution (alpha, lambda-zip, python3.14 arm64, VPC-attached, EventBridge rate(1 minute)). Live-validated end to end on a two-instance DocumentDB 5.0 lab through both Terraform and CloudFormation: attributed connections per instance, stable service.instance.id per cluster, dashboard verified against real data. The final Terraform run confirmed the DocumentDB security-group egress rule (failed_instances empty). One deviation from AC3 wording: an active connection's own effectiveUsers now takes precedence over the audit match rather than being a fallback, because the audit match for a reused socket can be stale. Screenshots ship sanitised (client address off, datasource picker hidden) and pass the public-release scan. doc-0003 is superseded by the example.
<!-- SECTION:FINAL_SUMMARY:END -->
