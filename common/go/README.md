# Go shared base

**Not built yet.** This directory is a placeholder with a decision recorded in
it, so the option stays open and correctly described.

## Go on Lambda, correctly

There is no managed Go runtime. `go1.x` was deprecated in January 2024, which
makes it look as though Lambda dropped Go - it did not. Go runs on the **OS-only
runtime**, `provided.al2023`, using
[`aws-lambda-go`](https://github.com/aws/aws-lambda-go) and the runtime
interface.

For longevity this is the **strongest** option on the whole runtimes page, not
the weakest:

- `provided.al2023` deprecates on the Amazon Linux clock, not a language-release
  clock. Its projected date is the furthest out of any Lambda runtime.
- A compiled binary has no language runtime to deprecate at all. Moving to the
  next `provided.alXXXX` is a rebuild with no source change.
- The zip is a single static binary, so cold starts are fast and there is no
  dependency tree in the artefact to patch.

See [`docs/runtimes.md`](../../docs/runtimes.md) for the full picture.

## What building this would involve

- A Go module here providing the same seams as the Python and Node bases: a Loki
  push client with structured metadata, a batcher, structured logging, config
  loading, and AWS helpers for S3 and Secrets Manager.
- `just` recipes wired into `fmt`, `fmt-check`, `lint` (`golangci-lint`) and
  `test`, following the same seven-recipe shape.
- `tools/package_example.py` learning to cross-compile
  (`GOOS=linux GOARCH=arm64`) and to name the handler binary `bootstrap`, which
  is what the OS-only runtime executes.
- The `provided.al2023` value already appears in the Terraform runtime
  allowlist, the CloudFormation `LambdaRuntime` parameter and
  `common/cloudformation/conformance.yaml`, so no IaC change is needed.

## Reach for Go when

The work is CPU-bound or memory-bound rather than IO-bound: decompressing very
large archives, parsing Parquet, or anything where a Python function needs more
than about 1 GB of memory to keep up. For streaming text out of S3, Python's
memory profile is already flat and the extra build complexity buys nothing.
