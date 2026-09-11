# Choosing and bumping a Lambda runtime

Read this before setting or changing a runtime in any example.

## The rule

**The runtime is never hardcoded.** It is one value, declared in
`examples/<name>/example.yaml` under `runtime.identifier`, and `just lint` fails
if the Terraform variable default or the CloudFormation parameter default
disagrees with it. A bump is a one-line manifest edit plus the same value in both
IaC paths, and the check proves you did all three.

**The default is always GA.** Preview runtimes are opt-in per deployment, gated
behind `allow_preview_runtime` (Terraform) and `AllowPreviewRuntime`
(CloudFormation).

## Why unplanned runtime work happens, and how to not have it

Lambda deprecates a *managed language runtime* when the language version reaches
end of community support. That is the clock that produces the surprise migration.
Three things follow:

1. **Pick the longest-lived GA runtime, not the newest.** The newest is often in
   preview, and among GA options the newest is not always the longest-lived.
2. **Prefer a compiled language on `provided.al2023` where the work suits it.**
   An OS-only runtime deprecates on the Amazon Linux clock, and a compiled binary
   has no language runtime to deprecate at all: moving to the next
   `provided.alXXXX` is a rebuild with no source change.
3. **Do not copy a deprecation date into code or docs.** AWS revises them. The
   table below exists so a reader understands the shape of the problem; the live
   table is the authority.

## The live table is the authority

<https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html>

Check it before setting a runtime. What follows is the state that shaped this
repo's defaults, not a fact to trust indefinitely.

| Runtime | Identifier | Projected deprecation |
| --- | --- | --- |
| Python 3.14 | `python3.14` | 2029-06-30 |
| Python 3.13 | `python3.13` | 2029-06-30 |
| OS-only (Go, Rust) | `provided.al2023` | 2029-06-30 |
| Python 3.12 | `python3.12` | 2028-10-31 |
| Node.js 24 | `nodejs24.x` | 2028-04-30 |
| Node.js 22 | `nodejs22.x` | 2027-04-30 |
| Python 3.15 | `python3.15` | preview, not scheduled |
| Node.js 26 | `nodejs26.x` | preview, not scheduled |

The same list, with GA/preview classification, lives in
`common/cloudformation/conformance.yaml`, which is what the lint enforces. Edit
that one file when a runtime is added or deprecated; do not restate it elsewhere.

## Public preview runtimes

AWS ships some runtimes in public preview before GA. They are available in every
commercial, GovCloud and China region, billed at standard rates, and patched on
the same cadence as GA runtimes. They are **not** covered by the Lambda SLA or
technical support, and AWS advises against production use. A preview runtime also
has no projected deprecation date, because it has no GA date yet.

So: never a default, always opt-in, and always with the deployer saying so in
their own configuration where a reviewer will see it.

```hcl
# Terraform
runtime               = "python3.15"
allow_preview_runtime = true
```

```
# CloudFormation
--parameter-overrides LambdaRuntime=python3.15 AllowPreviewRuntime=true
```

Without the gate, both IaC paths fail before creating anything, with the reason.
In Terraform that is a `lifecycle.precondition` on the function; in
CloudFormation it is a `WaitCondition` that cannot succeed, because
CloudFormation has no assertion primitive.

## Go and Rust: there is no managed runtime, and that is fine

`go1.x` was deprecated in January 2024. It is easy to read the current runtimes
table, see no Go entry, and conclude Lambda dropped Go. It did not.

Go and Rust run on `provided.al2023`, the OS-only runtime, via the runtime
interface (`aws-lambda-go`, or `lambda_runtime` for Rust). The handler binary is
named `bootstrap`. See [`common/go/README.md`](../common/go/README.md).

## When a deprecation notice arrives

1. Check the live table for the longest-lived GA runtime in the same language.
2. Change `runtime.identifier` in `example.yaml`, and the matching default in the
   example's `terraform/variables.tf` and `cloudformation/template.yaml`.
3. Update `common/cloudformation/conformance.yaml`: move the deprecated
   identifier out of `runtimes.ga`, and add the new one.
4. `just check`. The conformance check fails on any of the three places you
   missed, and on any template still offering the removed runtime.
5. `just package <name>` and release. A runtime change needs a redeploy, not just
   a configuration change, because the deployment package is built against a
   specific Python or Node version.
