---
id: doc-0002
title: Wave operating model
type: guide
created_date: '2026-09-26 15:37'
updated_date: '2026-09-26 15:37'
---
# Wave operating model - grafana-cloud-reference-examples

Repo-specific rules only. The fan-out protocol doc owns everything generic.

## Ownership

- Each directory under `examples/` is an independently releasable unit and is its own lane. Two lanes never edit the same example.
- `common/` is shared by every example, so a change there fans out as a minor bump on every consumer. A `common/` change is its own lane and lands before any example lane that depends on it.
- Packaging, release and conformance machinery (`tools/`, `scripts/`, `release-please-config.json`) belongs to one lane per wave.

## Gate

`just check` and `just package-all`, the same as CI's `ci-success`. An example that does not package is not done, even if checks pass.

## Exclusive resources

None. Nothing in this repo deploys; examples are downloaded and deployed by the customer.

## Run end

Report against this board (`backlog task list --plain`). Anything found but out of scope becomes a task here, not a note in a report.
