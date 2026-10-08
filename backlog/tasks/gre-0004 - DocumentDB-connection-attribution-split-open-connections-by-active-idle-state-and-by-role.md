---
id: GRE-0004
title: >-
  DocumentDB connection attribution: split open connections by active/idle state
  and by role
status: Done
assignee: []
created_date: '2026-10-08 08:26'
updated_date: '2026-10-08 09:29'
labels:
  - docdb-connection-attribution
dependencies: []
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Follow-up customer requirement on the docdb-connection-attribution example: show active, idle and total open connections per database user and per role, with the metric model, labels and PromQL documented. $currentOp already returns an 'active' flag per connection, which the collector projects and then drops. Roles are a property of the user, not of the connection, so they need a usersInfo lookup.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 docdb.connections.open carries a docdb.connection.state attribute (active|idle); sum without state gives total
- [x] #2 Roles exported as a docdb.user.role info gauge (1 per user/role/db) and joinable to the connections gauge in PromQL; missing viewUser privilege degrades to no role series with a logged warning, never a failed run
- [x] #3 Dashboard shows active, idle and total by user and by role; README documents the metric model, labels, PromQL and the active-is-a-sample caveat
- [x] #4 Validated live against a lab DocumentDB cluster with active and idle traffic, screenshots refreshed, lab torn down
- [x] #5 just check passes; CodeRabbit review clean of critical/major
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Shipped in 61bca04. docdb_connections_open now carries docdb_connection_state (active|idle) from $currentOp's active field. docdb_user_role is a value-1 info gauge, one series per user role grant, read once per run via usersInfo. Lab-verified on DocumentDB 5.0: each connection is one desc=Conn document, and only active ones carry effectiveUsers. clusterMonitor+read cannot run usersInfo (code 13). A custom role with only the viewUser action on admin can, and lacking it skips the role series with a warning. Role joins must filter one state per query, because keeping docdb_connection_state on both sides of the join is many-to-many and Prometheus rejects it. The dashboard is regrouped into Overview / Active and idle by user / Connections by role / Detail. The role row repeats over a hidden role_data variable (label_values(docdb_user_role, __name__)), so it disappears when there is no role data; both cases were rendered on robk. CodeRabbit: 0 findings. just check: green. Lab torn down: cluster, instance, SGs, subnet and parameter groups, audit log group, secrets, access policy, robk dashboards, Terraform stack. Teardown trap: terraform destroy stalls on the function security group while Lambda's VPC ENIs drain; delete the ENIs once they show available.
<!-- SECTION:FINAL_SUMMARY:END -->
