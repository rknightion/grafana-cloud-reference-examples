---
id: GRE-0002
title: Recognise Powertools Logger output in a CloudWatch Lambda logs to Loki example
status: To Do
assignee: []
created_date: '2026-09-29 13:40'
labels: []
dependencies: []
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Many customer Lambdas already log through the Powertools for AWS Lambda Logger, whose JSON output has fixed keys (level, service, cold_start, function_name, function_request_id, xray_trace_id, correlation_id). A CloudWatch Logs subscription to Loki example should detect that format and map it without a parse stage: service to a label (low cardinality), level to detected_level, and function_request_id, xray_trace_id, correlation_id and cold_start to structured metadata. That makes Powertools users' logs queryable immediately and gives a trace-to-logs link on xray_trace_id. No CloudWatch-to-Loki example exists yet; this is a requirement on it, not a standalone example.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 The CloudWatch-to-Loki example detects Powertools Logger JSON by its key set and maps it as described, tested against real Powertools Python and TypeScript output
- [ ] #2 No field from the mapping that varies per request becomes a label
- [ ] #3 The README documents the mapping and a LogQL query joining on xray_trace_id
<!-- AC:END -->

## Definition of Done
<!-- DOD:BEGIN -->
- [ ] #1 just check
- [ ] #2 just package-all
<!-- DOD:END -->
