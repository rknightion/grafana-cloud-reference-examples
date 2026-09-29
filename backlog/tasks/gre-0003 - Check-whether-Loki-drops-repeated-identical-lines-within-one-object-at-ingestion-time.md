---
id: GRE-0003
title: >-
  Check whether Loki drops repeated identical lines within one object at
  ingestion time
status: To Do
assignee: []
created_date: '2026-09-29 13:40'
labels: []
dependencies: []
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
generic-s3 stamps every line of an object with one time.time_ns() value when TIMESTAMP_FIELD is unset. Loki drops an entry whose timestamp and line match one it already holds. If that ingest-time check ignores structured metadata, two identical lines in one file (a repeated heartbeat, a duplicated CSV row) collapse to one and data is silently lost. The record number in structured metadata may or may not save them: upstream says read-path dedup compares structured metadata, the ingest path is unconfirmed. adobe-aem is exposed only on lines that carry no timestamp or are restamped by MAX_TIMESTAMP_AGE_SECONDS. Found while correcting the at-least-once docs.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Behaviour confirmed against Loki source or a local Loki: whether identical line plus timestamp with different structured metadata is kept at ingest
- [ ] #2 If lines are dropped, ingestion-time stamping gives each line of an object a distinct timestamp (for example now_ns plus the line index) and a test pins it
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->
