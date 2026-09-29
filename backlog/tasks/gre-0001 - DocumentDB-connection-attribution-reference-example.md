---
id: GRE-0001
title: DocumentDB connection attribution reference example
status: To Do
assignee: []
created_date: '2026-09-29 09:12'
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
- [ ] #1 examples/docdb-connection-attribution exists with the full required file set, a status other than planned, and passes just check and just package
- [ ] #2 The attributor attributes idle connections by joining $currentOp client ip:port to audit authenticate remote_ip, queries every instance endpoint with directConnection, and exports docdb.connections.open with cluster, instance, user and app as data point attributes (not resource attributes)
- [ ] #3 Unit tests cover the join: latest authenticate event wins for a reused ip:port, failed authentications are ignored, an active connection falls back to effectiveUsers, and an unmatched connection is counted as <unattributed>
- [ ] #4 The README states the least-privilege DocumentDB role (clusterMonitor plus read on admin), that audit_logs=ddl is sufficient, that attaching a custom parameter group needs a reboot, and why unattributed connections exist
- [ ] #5 The README explains why the MongoDB Enterprise data source cannot do this, and documents its tls=true connection-string trap for customers who use that data source for other queries
- [ ] #6 A dashboard ships with the example and works with the cluster filter, verified against real exported data rather than only rendered
- [ ] #7 Client address is opt-in and off by default, and the README names its cardinality cost
- [ ] #8 A decision is recorded on whether and how screenshots ship (sanitised, with no private IPs), and if they ship they pass the public-release scan
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->
