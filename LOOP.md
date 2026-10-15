# Loop: grafana-cloud-reference-examples
tier: routine
gate: just check
ci-required: ci-success
grafana-stack: none

Public repository: no credential, stack, org or tenant ID, email, public IP or internal hostname in
any tracked file or loop artifact. Nothing here deploys; customers download and deploy the examples.
The gate is `just check` plus `just package-all`, the same as CI's `ci-success`: an example that
does not package is not done even when checks pass.

## Credentials

- None held by the loop.

## Traps

- `common/` is shared by every example, so a change there fans out as a minor bump on every consumer.

## Mutexes

- Each directory under `examples/` is an independently releasable unit and its own lane: two lanes
  never edit the same example.
- `common/` is its own lane and lands before any example lane that depends on it.
- Packaging, release and conformance machinery (`tools/`, `scripts/`, `release-please-config.json`)
  belongs to one lane at a time.

## Lane brief

Anything found but out of scope becomes a task on this board, not a note in a return.
