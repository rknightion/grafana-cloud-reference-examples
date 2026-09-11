# Contributing

```bash
just setup     # Python workspace, Node workspace, Terraform providers, tflint plugins
just check     # the gate: fmt, lint, test, IaC validate, conformance
```

`just check` must pass before a commit. `just --list` shows everything else.

- **Adding an example?** [`docs/adding-an-example.md`](docs/adding-an-example.md).
- **Changing the shared code, packaging or release flow?**
  [`docs/architecture.md`](docs/architecture.md).
- **Conventions, and the things not to get wrong?** `AGENTS.md` in the repo root.

## Commits

Conventional Commits, and the scope must equal the release component, because
release-please routes on it:

```
feat(generic-s3): stream gzipped objects without buffering
fix(common-python): retry Loki 429 with the Retry-After delay
docs: explain why OTLP is not a second sink
```

A wrong scope releases the wrong package. The valid scopes are the `component`
values in `release-please-config.json`.

## Three things reviewers will always ask

1. **Is any new label low-cardinality?** Anything derived from an object key, a
   record id or a timestamp belongs in structured metadata.
   [`docs/loki-ingestion.md`](docs/loki-ingestion.md).
2. **Could this reuse `common/` instead?** A forked Loki client is how the retry
   semantics diverge.
3. **Does the README tell a customer what it costs to run?** It ships inside the
   release bundle and is the only documentation they read.
