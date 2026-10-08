# How the repo fits together

Read this before changing the packaging, the release flow or the conformance
checks.

## The shape

```
common/            shared code and IaC. Never released on its own
examples/<name>/   one deliverable per directory, flat
tools/             three programs, each with exactly one just recipe
docs/              this
```

Four decisions explain most of it.

## 1. `examples/` is flat

Platform, runtime, status and deliverable are fields in `example.yaml`, not
directory levels. So `ls examples/` is the CI matrix, the release tag is the
directory name, and nothing has to be re-derived from a path.

A `examples/aws/...` layout would put the platform in two places - the path and
the manifest - and the two would eventually disagree.

## 2. Shared code is vendored, never published

`common/python` and `common/nodejs` are workspace members. An example imports
them directly, so they resolve under the editor, pytest, mypy, tsc and vitest
with no publish step. `just package` copies the resolved source into the release
zip.

The alternatives were considered and rejected:

- **PyPI or npm.** A shared fix then needs a release plus a version bump in every
  consumer, and a customer reading the zip cannot see the code that is running.
- **A Lambda layer.** Smallest function zips, but the layer becomes a deploy
  prerequisite - which breaks the one-zip-is-the-whole-deliverable property that
  makes these examples useful.

The core of each library depends on nothing: Python's is standard library only,
Node's uses global `fetch` and `node:zlib`. AWS helpers are a separate subpackage
and entry point. A minimal example's deployment package is a few tens of
kilobytes with no third-party CVE surface, and a test in each library asserts the
split statically so it cannot rot.

## 3. Terraform reuses by module; CloudFormation reuses by copy plus enforcement

`common/terraform/modules/*` are real modules, sourced by relative path and
vendored at package time with their `source` rewritten to `./modules/<name>`.

CloudFormation cannot do the equivalent. A nested stack needs its children staged
in S3 before the root stack can be created, which breaks the self-contained zip.
So each example carries a full standalone template, and
`common/cloudformation/conformance.yaml` lists what every template must have.
`just lint` enforces it. Templates may diverge on what the example does; they may
not diverge on a finite log retention, a DLQ, `ReportBatchItemFailures`, a closed
runtime allowlist with a GA default, a preview-runtime gate, wildcard-free IAM, or
credential handling.

See [`common/cloudformation/README.md`](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/common/cloudformation/README.md).

## 4. One value per fact, checked

The runtime identifier appears in three files. It has to: Terraform needs a
variable default, CloudFormation needs a parameter default, and the manifest is
what the tooling reads. So `tools/check_conformance.py` cross-checks all three
and fails if they disagree. Same for `release.component` versus `name`.

That is the pattern for anything that must be stated more than once: pick the
authoritative copy, and make the gate prove the others match.

## The tools

Three programs. Each has exactly one `just` recipe, which is the only supported
way to run them.

| Tool | Recipe | What it does |
| --- | --- | --- |
| `check_conformance.py` | `just lint` | Manifest schema, file set, runtime agreement, CFN conformance |
| `package_example.py` | `just package <name>` | Builds the release bundle |
| `new_example.py` | `just new-example <name>` | Scaffolds an example |

### Deterministic packaging

Every zip entry gets a fixed timestamp (1980-01-01) and normalised permissions,
and entries are sorted. Two builds of the same commit are byte-identical.

This is not tidiness. Terraform detects new function code via
`source_code_hash`; a zip whose hash changes on every build makes every `apply`
redeploy unchanged code, and makes the `MANIFEST.json` hash useless for
verifying what a customer downloaded.

`tools/tests/test_tools.py` asserts it.

### Cross-platform builds without Docker

Third-party wheels are installed with `uv pip install --python-platform
aarch64-manylinux2014 --python-version <x.y> --only-binary :all:`, so a macOS
developer and a Linux CI runner produce the same wheel set. Requirements come
from `uv.lock` rather than the pyproject ranges, so the bundle pins exactly what
CI tested.

### The runtime-provided SDK is a per-example decision

`lambda.rely_on_runtime_aws_sdk` in the manifest excludes boto3 and its
dependencies from the deployment package and uses the copy the Lambda Python
runtime provides. It takes `generic-s3` from about 16 MB to about 26 KB.

AWS recommends bundling the SDK so an automatic runtime update cannot change its
version underneath you, and that is right for a function leaning on recent SDK
behaviour. An example calling only `GetObject` and `GetSecretValue` is not, so it
opts out. The choice is per example and stated in the manifest with its reason -
never a packaging default.

## Releases

release-please in manifest mode, one component per example plus one per shared
library. Tags are `<component>-vX.Y.Z`, so `generic-s3-v1.2.0` only ever changes
`generic-s3`.

The commit scope must equal the component name:

```
feat(generic-s3): stream gzipped objects without buffering
fix(common-python): retry Loki 429 with the Retry-After delay
```

A change to `common/` fans out as a minor bump on every consuming example,
because the vendored code in their bundles changed.

On a release, CI builds the bundle for each released component and attaches it to
that component's GitHub Release, along with the bare `lambda.zip` for anyone who
only needs the function code. Each bundle is built from a checkout of its own
release tag, not the commit that triggered the run, because one run can cut
several releases when release merges land back to back. The job fails if the
built version does not match the tag.

To rebuild and re-attach the bundle for an existing release, dispatch the
`release` workflow with its tag:

```
gh workflow run release.yml -f tag=generic-s3-v1.2.0
```

### Non-breaking releases ship themselves

On every push to `main`, the `arm-automerge` job in `release.yml` arms GitHub
auto-merge on each open release PR. The PR then merges itself once `ci-success`
passes on its current head. A release PR is held instead, labelled
`release: manual` and disarmed if it was already armed, when:

- its changelog has a `BREAKING CHANGES` section, which is the only signal that
  works pre-1.0, because `bump-minor-pre-major` makes a breaking change a minor
  bump;
- its major version differs from the manifest; or
- its title cannot be resolved to a component and current version.

A held PR is released by merging it by hand. To hold a non-breaking release,
disable auto-merge on the PR; the next push to `main` re-arms it, so convert it
to a draft if it must wait longer.

Every release PR edits `.release-please-manifest.json`, so each merge leaves the
other open release PRs conflicting. `always-update` in the release-please config
makes release-please rewrite every open release PR on each push; without it, it
pushes only when a PR's body changes, so a conflicted PR stays conflicted and
never merges.

Auto-merge is armed with the release App token, never `GITHUB_TOKEN`. A merge
armed by `GITHUB_TOKEN` fires no `push`, so release-please would never run to cut
the tag.

## Adding a new kind of deliverable

`deliverable` in the manifest is an enum: `lambda-zip`, `container-image`,
`script`, `terraform-module`, `helm-chart`. Only `lambda-zip` is implemented.

Adding one means teaching `package_example.py` how to build it and
`check_conformance.py` which files it requires. Do that rather than scripting a
second build path beside the packager - the whole point of one packager is that
every deliverable is reproducible and hashed the same way.
