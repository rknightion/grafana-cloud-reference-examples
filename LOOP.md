# Loop: grafana-cloud-reference-examples
tier: routine
gate: just check
ci-required: ci-success
grafana-stack: none
tracker: github
landing: pr

Public repository: no credential, stack, org or tenant ID, email, public IP or internal hostname in
any tracked file or loop artifact. Nothing here deploys; customers download and deploy the examples.
The gate is `just check` plus `just package-all`, the same as CI's `ci-success`: an example that
does not package is not done even when checks pass.

The tracker working model is `~/repos/agent-docs/docs/github-tracker.md` (maintainers only).

## Queue

- The queue is `just tracker queue`: open issues at Project Status Ready on the rknightion Project.
- A charter selects work by labels (`type:task`, `type:bug`, `type:feature`, `needs-owner`,
  `needs-triage`) and Project fields (Status, Priority, Effort, Lane). Never queue a `needs-owner` or
  `needs-triage` issue.
- Only the root writes issues and the Project, through `just tracker`. A lane returns the status and
  the AC ticks it wants.
- A queue row names the issue as `#N`. A legacy board id (`GRE-0001`) resolves with `just issue <ID>`;
  `.github/tracker/id-map.json` is the map.

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

Anything found but out of scope becomes an issue (the root files it with `just tracker create`), not a note in a return.
