---
id: GRE-0004
title: >-
  DocumentDB connection attribution: split open connections by active/idle state
  and by role
status: To Do
assignee: []
created_date: '2026-10-08 08:26'
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
- [ ] #1 docdb.connections.open carries a docdb.connection.state attribute (active|idle); sum without state gives total
- [ ] #2 Roles exported as a docdb.user.role info gauge (1 per user/role/db) and joinable to the connections gauge in PromQL; missing viewUser privilege degrades to no role series with a logged warning, never a failed run
- [ ] #3 Dashboard shows active, idle and total by user and by role; README documents the metric model, labels, PromQL and the active-is-a-sample caveat
- [ ] #4 Validated live against a lab DocumentDB cluster with active and idle traffic, screenshots refreshed, lab torn down
- [ ] #5 just check passes; CodeRabbit review clean of critical/major
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->
