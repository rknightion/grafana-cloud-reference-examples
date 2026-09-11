# Adding an example

## Start it

```bash
just new-example my-thing          # python is the default
```

That writes a complete example that already passes the structural checks:
manifest, README skeleton, handler, tests, a Terraform root module wired to the
shared modules, and a CloudFormation template copied from the canonical
reference. Three edits are then required, and the generator prints them:

1. `pyproject.toml` - add `"examples/my-thing"` to `tool.uv.workspace.members`
2. `release-please-config.json` - add the package entry, and the same path with
   version `0.1.0` in `.release-please-manifest.json`
3. `examples/my-thing/example.yaml` - fill in `title` and `summary`

Then `just setup && just check`.

These three are deliberately not generated. Each is a reviewed edit to a file
that governs the whole repo, and a generator silently appending to them is how a
release ends up publishing the wrong thing.

## The manifest is the source of truth

`example.yaml` decides the runtime, the deliverable, the release component and
the file set. `just lint` cross-checks `runtime.identifier` against the Terraform
variable default and the CloudFormation parameter default, so a runtime bump
applied in one place and missed in the other two fails the gate rather than
deploying the old runtime silently.

`status: planned` exempts an example from the required-file-set check, and from
the README heading contract. Nothing else is exempt. Use it for something
announced but not written, and flip it once the code exists - at which point both
checks start applying.

## Write the README for a customer, not for us

The README ships inside the release bundle and is the only documentation a
customer reads. **`just check` enforces the heading set and their order**, so
this is a contract rather than a suggestion - a customer who has deployed one
example should find the next in the same shape.

Required headings, in order:

| Heading | What goes in it |
| --- | --- |
| `## What is in this download` | The bundle's file tree. Not a required heading, but write it: it orients someone who has just unzipped. |
| `## What you need before you start` | A numbered list of prerequisites, each one actionable. Endpoint, tenant id, token, the `aws secretsmanager create-secret` command, the tool versions. Anything the customer must go and get elsewhere. |
| `## What this creates in your AWS account` | The resource list, **and what it explicitly does not touch.** "It never modifies or deletes your objects" is worth stating. |
| `## Deploy it` | Both IaC paths, with real copy-pasteable commands. |
| `## Check it worked` | Numbered steps that each narrow the problem down: make data flow, find it in Loki, confirm the fields parsed, confirm nothing is stuck. **This is the section people skip and the one customers need most.** |
| `## Configuration` | A table of every environment variable and its default. Then labels, and what deliberately is not one. |
| `## What it costs` | Loki ingest dominates; also Lambda GB-seconds, CloudWatch Logs and per-request charges. Name the lever that actually reduces it. |
| `## Troubleshooting` | Symptom first, in bold, then cause and fix. Cover 401, a too-old timestamp, 429, an empty query result, and the dead-letter queue. |
| `## Limitations` | Honestly. A named limitation is worth more than a vague reassurance. |
| `## How it works` | Design rationale, **last**. The customer wanting to deploy should not have to read past it. |

Two more rules, both checked:

**No relative link may climb out of the example directory.** `just package`
copies exactly one README into the bundle, beside `lambda.zip`, `terraform/` and
`cloudformation/`. A `../../docs/x.md` link resolves to nothing on the customer's
disk while still working in the repository, which is why it goes unnoticed. Use
an absolute `https://github.com/rknightion/grafana-cloud-reference-examples/...`
link, or inline the content. Prefer inlining anything a customer needs while
following the steps; save the link for depth.

**Every `terraform output -raw <name>` you mention must exist.** Telling a
customer to run an output the module does not declare wastes their time on an
error that looks like their own mistake.

Lead with the answer, not the reasoning. The customer wants to know what this
is, whether it fits, and how to deploy it; the design argument is for the
engineer who comes back later.

[`generic-s3`](../examples/generic-s3) and
[`adobe-aem`](../examples/adobe-aem) are the worked versions.

## Reuse, do not fork

Before writing anything, check what already exists:

| Need | Where it lives |
| --- | --- |
| Push to Loki, batch, retry, validate labels | `grafana_cloud_common` / `@grafana-cloud/common` |
| Stream and decompress an S3 object | `grafana_cloud_common.aws.S3ObjectReader` |
| Resolve the credential from Secrets Manager | `grafana_cloud_common.aws.CredentialProvider` |
| Parse S3, SQS-wrapped or EventBridge events | `grafana_cloud_common.aws.parse_event` |
| Isolate per-message failures | `grafana_cloud_common.aws.process_messages` |
| The function, its role, log group and alarms | `common/terraform/modules/lambda-function` |
| Queue, DLQ, bucket notification, event mapping | `common/terraform/modules/s3-event-source` |
| Grant read access to the credential secret | `common/terraform/modules/grafana-cloud-credentials` |
| Test doubles for the Loki client and S3 reader | `grafana_cloud_common.testing` |

If an example needs something the shared code nearly does, change the shared
code. A fork of the Loki client is how the retry semantics diverge and one
example quietly stops honouring `Retry-After`.

Something used by exactly one example belongs in that example, not in `common/`.
A module with one caller is indirection, not reuse.

## What to test, and what not to

Test the parsing and the label derivation. That is where the bugs are, and the
label check is worth writing because `validate_labels` turns a cardinality
mistake into a test failure rather than a bill.

Do not retest the S3 reading, the batching, the Loki push or the partial-failure
handling. `common/python/tests` and `common/nodejs/test` cover those, and a copy
in each example makes changing the shared code expensive for no extra coverage.

Do not write a unit test for a Terraform variable or a CloudFormation parameter.
`just lint` validates those; a test asserting a default is a second copy of the
same fact.

## Before you open it up

```bash
just check                  # the whole gate
just package my-thing       # the real release bundle
unzip -l dist/my-thing-0.1.0.zip
```

Read that listing. It is what a customer downloads: the README, the LICENSE, a
`MANIFEST.json` with the zip's SHA-256, `lambda.zip`, a `terraform/` tree with
the shared modules vendored in and their sources rewritten to `./modules/<name>`,
and a `cloudformation/` directory. Nothing should reference a path outside the
bundle.

Building twice produces byte-identical archives. If it does not, something in the
packager has picked up a timestamp or a machine-specific path, and Terraform will
redeploy unchanged code on every apply.

## Releasing it

Commit with the component as the scope, because release-please routes on it:

```text
feat(my-thing): initial release
```

A wrong scope releases the wrong package. `release.component` in the manifest
must equal `name`, and `just lint` checks that.
