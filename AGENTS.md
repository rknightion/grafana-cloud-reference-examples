# grafana-cloud-reference-examples

Self-contained, independently releasable reference examples for getting data into Grafana Cloud and
for integrating Grafana Cloud with other products. Each example is something a customer can download
as one zip and deploy. Nothing here is a product, and nothing here is deployed by this repo.

## Task interface

`just check` is the gate. Run it before every commit. `just --list` shows the rest.

There is no `Makefile` and no `scripts/*.sh` task runner. Do not add one.

## Layout contract

```
common/            shared, reused by examples. Never released on its own.
  python/          uv workspace package `grafana-cloud-common`
  nodejs/          npm workspace package `@grafana-cloud/common`
  go/              not built yet; see common/go/README.md
  terraform/modules/    real reusable modules
  cloudformation/       reference template + conformance rules (NOT nested stacks)
  schemas/         JSON Schema for example.yaml
examples/<name>/   one deliverable per directory, flat, no platform nesting
tools/             real programs invoked only through a just recipe
docs/              contributor and customer documentation
```

`examples/` is flat on purpose. Platform, runtime and status are fields in `example.yaml`, not
directory levels, so the CI matrix and the release tag are both derivable from `ls examples/`.

Every non-planned example has exactly this shape, and `just lint` fails if it does not:

```
examples/<name>/
  example.yaml         manifest; the single source of truth for name, runtime, platform, status
  README.md            customer-facing: what it does, what it costs, how to deploy
  src/                 handler code
  tests/
  terraform/           root module, plus terraform.tfvars.example
  cloudformation/      one flat template.yaml, plus parameters.example.json
```

An example that is announced but not yet written sets `status: planned` in `example.yaml` and is
exempt from the file-set check. Nothing else is exempt.

## Never hardcode a Lambda runtime identifier

Managed language runtimes are the thing that forces unplanned work later. The runtime is one value
per example, declared once in `example.yaml`, and `just lint` fails if the Terraform variable default
or the CloudFormation parameter default disagrees with it. Bumping a runtime is a one-line manifest
change plus a rebuild.

Default to the longest-lived GA runtime, never a preview one. Preview runtimes carry no SLA and no
technical support, so they are opt-in per deployment, never the shipped default.

Prefer `provided.al2023` for anything compiled (Go, Rust). It deprecates on the OS clock rather than
a language-release clock, and a compiled binary has no language runtime to deprecate at all.

Read `docs/runtimes.md` before choosing or changing a runtime. Do not restate the deprecation table
anywhere else; it rots.

## Shared code is vendored, never published

`common/python` and `common/nodejs` are workspace members, so an example imports them directly and
they resolve under the editor, pytest, mypy and tsc. `just package <name>` copies the resolved
library into the release zip.

Do not publish either to PyPI or npm, and do not turn them into a Lambda layer. Both break the
one-zip-is-the-whole-deliverable property that makes these examples useful.

`common/python`'s core modules depend on the standard library only, so a minimal example zip carries
no third-party code. AWS-specific helpers live under `grafana_cloud_common.aws` and may import
`boto3`. Keep that split.

## Terraform

Shared modules are sourced by relative path in-repo (`../../../common/terraform/modules/<m>`).
`tools/package_example.py` copies them into the zip and rewrites the source to `./modules/<m>`, so
the packaged tree has no path escaping its own directory. A module source that is an absolute path,
a registry reference or a git URL breaks packaging.

No backend block in any example root module. The customer chooses their own state backend.

## CloudFormation is flat, single-file, and deliberately duplicates the modules

Each example ships one standalone `template.yaml`. Nested stacks are not used: they require the child
templates to be staged in an S3 bucket first, which means the zip is no longer deployable on its own.

Reuse is enforced rather than factored. `common/cloudformation/templates/lambda-s3-loki.reference.yaml`
is the canonical base to copy from, and `common/cloudformation/conformance.yaml` lists the pieces every
example template must carry. `just lint` checks each template against it.

## The publication constraint

`scripts/public-release-scan.sh`, also `just public-release-scan` and the first stage of
`just check`, scans the working tree **and every reachable Git revision** for credential material,
environment identity and forbidden filenames.

**Because it scans history, a banned literal that reaches a commit is not fixed by a later commit
that removes it.** This repository is published, so the blob stays fetchable. The gate has to fail
before the commit.

The trap that catches agents specifically is **absolute local paths**. The home-directory prefixes
are rejected case-sensitively, and tooling instructions, pasted command lines and generated docs
carry one by default. Derive paths from `git rev-parse --show-toplevel` or use relative paths. Never
hard-code one, in this file included.

Also rejected: private hostnames, private repository names, private IPv4 endpoints, and a real
account or tenant id. The examples use the documented placeholders throughout - `123456789012` for
an AWS account, `123456` for a Loki tenant, `logs-prod-012.grafana.net` for an endpoint - and those
are the only allowed forms.

Two things make the scan trustworthy rather than decorative: it refuses to run without `ripgrep`
instead of matching nothing, and a search that errors aborts the scan instead of reading as a pass.
Do not "fix" either by making it lenient.

## Secrets

Grafana Cloud credentials are a Cloud Access Policy token plus a numeric tenant id. The token reaches
the function through Secrets Manager (or SSM SecureString), fetched once per cold start and cached.

Never put a token in a Terraform variable default, a CloudFormation parameter default, a
`.tfvars.example`, a `parameters.example.json`, or an environment variable in any committed template.
`just lint` fails on a parameter named like a secret that has a non-empty default.

## Loki label discipline

Labels are for low-cardinality dimensions only. A label whose value comes from an S3 object key, a
request id, a customer id, a timestamp or a filename creates one stream per value and is a cost and
performance incident, not a style preference. Put that data in the log line or in structured metadata.

`docs/loki-ingestion.md` has the allowed label set and the reasoning.

## Releases

release-please in manifest mode, one component per example plus one per shared library. Tags look
like `generic-s3-v1.2.0`. The commit scope must equal the component name or the release lands on the
wrong package:

```
feat(generic-s3): stream gzipped objects without buffering
fix(common-python): retry Loki 429 with the Retry-After delay
```

A change to `common/` fans out as a minor bump on every consuming example.

## Local conventions

- Commit straight to `main` and push. Human changes do not go through a PR.
  release-please is the one exception: it opens a release PR per component, and
  merging that PR is what creates the tag. Do not hand-edit or hand-tag a
  release.
- Python 3.14 locally, matching the default runtime. Node 24.
- `uv` and `npm ci` only. No `pip install` into the workspace.

## Deeper references

- `docs/runtimes.md` - read before choosing or bumping a Lambda runtime
- `docs/adding-an-example.md` - read before creating a new example directory
- `docs/loki-ingestion.md` - read before choosing labels, batching or timestamps
- `docs/grafana-cloud-credentials.md` - read before wiring auth or minting a Cloud Access Policy token
- `docs/otlp-vs-loki-push.md` - read before proposing OTLP as an output for an example
- `docs/architecture.md` - read before changing the packaging, release or conformance machinery
- `common/terraform/modules/README.md` - read before adding or changing a shared module
- `common/cloudformation/README.md` - read before adding a CloudFormation template
