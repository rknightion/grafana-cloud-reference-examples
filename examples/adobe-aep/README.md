# adobe-aep: Adobe Experience Platform exports to Grafana Cloud Loki

**Status: planned.** Nothing here is implemented yet. This README records the
shape the example will take and the decisions already made, so the work starts
from a position rather than a blank page.

Start with [`generic-s3`](../generic-s3), which is the working example and the
base this one extends.

## What it will do

Adobe Experience Platform exports datasets and profile segments to a cloud
storage destination. Where that destination is Amazon S3, the objects land under
a partitioned prefix with an accompanying manifest. This example picks them up
and ships the records to Grafana Cloud Loki.

Everything format-agnostic is already in `generic-s3` and in
`common/python/grafana_cloud_common`. This example adds only the parts that are
specific to AEP.

## What is already decided

- **Extends `generic-s3`, does not fork it.** The S3 streaming reader, the Loki
  client, the batcher, the credential provider and the partial-batch-failure
  handling are shared code in `common/python`. Only the parsing and labelling is
  new here.
- **`python3.14`, arm64.** Same as every other Python example, pinned in
  `example.yaml` and cross-checked against both IaC paths by `just lint`.
- **1024 MB and a 600 second timeout**, higher than `generic-s3`'s defaults,
  because AEP export objects are larger and Parquet decoding is CPU-bound rather
  than streaming.
- **Direct Loki push, not OTLP.** See
  [`docs/otlp-vs-loki-push.md`](../../docs/otlp-vs-loki-push.md) - for arbitrary
  third-party records, OTLP's resource-attribute promotion model indexes the
  wrong things.

## What is not decided, and needs answering first

These are the real questions. Each one changes the code.

1. **Which export format.** AEP can export as JSON, and commonly exports as
   **Parquet**. Parquet is not streamable with the standard library and needs
   `pyarrow`, which is a large dependency for a Lambda zip - large enough that a
   container image deliverable may be the better answer than a zip. Decide the
   format before anything else; it determines the deliverable.
2. **Whether to read the export manifest.** An AEP export writes a manifest
   alongside the data files. Using it gives an exact file list and a completeness
   signal, so a partial export is not shipped as if it were whole. Ignoring it is
   simpler and processes each object as it arrives. The manifest is the more
   correct answer and the more work.
3. **XDM field mapping.** XDM records are deeply nested. Which fields become Loki
   labels is the single most consequential decision in this example, and the
   constraint is fixed: labels are low-cardinality only. An identity, a profile
   id, a segment membership id or a timestamp is one Loki stream per value. Those
   belong in structured metadata. A dataset id, a sandbox name, a schema name and
   an environment are reasonable labels.
4. **Timestamps.** XDM records carry their own event time, often well in the
   past. Loki rejects samples older than the tenant's
   `reject_old_samples_max_age` with a 400 that no retry fixes, so a backfill
   needs either a raised tenant limit or ingestion-time stamping.
   `generic-s3`'s `MAX_TIMESTAMP_AGE_SECONDS` already implements the fallback.
5. **Whether records should go to Loki at all.** A profile export is closer to
   analytical data than to logs. If the intent is aggregate reporting rather than
   record-level search, Mimir or a different surface may be the right destination
   and this becomes a different example.

## Getting started on it

Answer question 1 first, because the rest follows from it. Only then write the
code - a Parquet answer probably makes this a container-image deliverable rather
than a lambda-zip, which changes `example.yaml` before any code exists.

`just new-example` cannot be used here: it refuses to write into a directory that
already exists, deliberately, so it can never overwrite work. Copy the shape from
[`generic-s3`](../generic-s3) instead, and when the code lands:

1. Add `"examples/adobe-aep"` to `tool.uv.workspace.members` in the root
   `pyproject.toml`.
2. Add the `examples/adobe-aep` package to `release-please-config.json`, and the
   same path at `0.1.0` in `.release-please-manifest.json`.
3. Add `destinations` and `release.component` to `example.yaml`, and change
   `status` from `planned`. The conformance check then starts enforcing the full
   file set.
4. `just check`.
