# Pushing to Loki: labels, timestamps, batching

Read this before choosing labels, setting timestamps, or tuning batch sizes.

## Labels are the whole game

A Loki **stream** is one unique combination of label values. Every distinct
combination gets its own index entry, its own set of chunks, and its own write
path. Cardinality is therefore not a style question: it is the thing that decides
whether a query takes 200 ms or times out, and whether ingestion keeps up or
spends its life being rate-limited.

**A label derived from an object key is one stream per file.** A bucket receiving
10,000 files a day produces 10,000 streams a day, forever. That is an incident,
not a preference.

### What belongs in a label

Low-cardinality dimensions you would actually select on:

```logql
{service_name="acme-app-logs", bucket="acme-logs", env="prod"}
```

- `service_name` - what produced this
- `bucket` - where it came from
- `env`, `region`, `tenant` - deployment dimensions with a handful of values
- `prefix` - a *shallow* key prefix, one or two segments, where the layout is
  `<team>/<source>/<date>/...` and you stop before the date

### What does not

Anything whose value varies per record, per file, or per request. The client
**rejects** these rather than letting you find out from the bill:

`object_key`, `filename`, `path`, `key`, `request_id`, `trace_id`, `span_id`,
`user_id`, `customer_id`, `session_id`, `uuid`, `id`, `ip`, `timestamp`, `time`,
`date`, `message`, `line`

There is also a ceiling of 15 labels per stream. Loki's own server-side limit is
higher; a stream with 15 labels is almost always a mistake being made at scale.

## Structured metadata is where high-cardinality data goes

Loki 3 has **structured metadata**: per-line key/value pairs that are stored and
queryable but do **not** create a stream. It is the third element of an entry in
the push payload.

```json
{"streams":[{"stream":{"service_name":"x"},"values":[
  ["1757548800000000000","the log line",{"object_key":"logs/2026-09-11/a.gz","record":"41"}]
]}]}
```

Query it after the stream selector:

```logql
{service_name="acme-app-logs"} | object_key=~"logs/2026-09-.*" | record="41"
```

So the answer to "but I need to know which file a line came from" is always
structured metadata, never a label. Every example in this repo puts the object
key, the record number and the object version there.

Limits worth knowing: roughly 128 keys and 64 KB of structured metadata per line.
Entirely sufficient for provenance; not a place to put a copy of the record.

## Timestamps

Entries are epoch **nanoseconds**, as a string in the payload.

> In the Node library this is a `bigint`. A nanosecond epoch is 19 digits and a
> JS double carries about 16, so a `number` silently rounds the timestamp.
> Python's `int` is arbitrary-precision and has no such problem.

**Default to ingestion time.** Loki rejects a sample older than the tenant's
`reject_old_samples_max_age` - a week on Grafana Cloud unless you have had it
raised - with a **400**, which no amount of retrying fixes. A backfill of last
year's exports fails entirely, one batch at a time.

Extract timestamps from the records when you want event time and the data is
recent. `generic-s3` does this with `TIMESTAMP_FIELD` plus `TIMESTAMP_FORMAT`,
and `MAX_TIMESTAMP_AGE_SECONDS` falls back to ingestion time for anything older
rather than having the batch rejected.

For a genuine historical backfill, get the tenant limit raised first. There is no
client-side trick for it.

**Sort ascending within a stream.** Loki tolerates some out-of-order ingestion,
but an unsorted batch can be partially rejected with a 400 naming only the first
offending line, which is miserable to debug. Both client libraries sort for you.

## Batching

Two bounds, and both matter:

| Bound | Default | Why |
| --- | --- | --- |
| Lines per push | 5,000 | Amortises the round trip |
| Approximate bytes per push | 4 MiB | Keeps a 128 MB function inside memory, and inside the server's body limit |

5,000 tiny lines is a fine batch. 5,000 lines of a 200 KB JSON blob each is 1 GB.
An entry larger than the byte bound is still sent on its own rather than dropped:
losing a customer's log line because it is large is worse than one oversized
request, and Loki says clearly if it refuses.

## Rate limits, retries and duplicates

Grafana Cloud enforces a per-tenant ingest rate. A large fan-out - one Lambda per
object across a big S3 prefix - hits it, and every function then retries at once.

Three things handle this:

- **429 and 5xx are retried; 4xx is not.** A 400 from a bad label set or an
  out-of-window timestamp can never succeed, and retrying it wastes the budget.
- **`Retry-After` is honoured** when Loki sends it, because the server's own
  number beats any local guess.
- **Backoff uses full jitter.** Without jitter, a fan-out retries in lockstep and
  hits the same limit at the same instant.

Set `reserved_concurrency` before pointing an example at a bucket with a large
existing backlog. It is the only thing that actually bounds the fan-out.

**Duplicates after a failure are expected and harmless.** Loki deduplicates
entries identical in timestamp, labels and line content within a stream, and a
redelivered SQS batch produces exactly that. This is also why
`ReportBatchItemFailures` matters: without it a single bad object forces
redelivery of the whole batch, and while the duplicates are deduplicated, the
ingest is paid for twice.

## Why JSON rather than protobuf

Loki's push API accepts protobuf + snappy, which is meaningfully more efficient at
high throughput. Both libraries here use JSON, because:

- The Python core is standard-library only and the Node core has no runtime
  dependencies, which keeps a minimal deployment package to a few tens of
  kilobytes with no third-party CVE surface.
- These examples are read as much as they are run, and a JSON body can be
  inspected with `curl` and `jq`.
- gzip on the request body recovers most of the wire-size difference.

Reach for protobuf when you are sustaining tens of MB/s to one tenant and have
measured the JSON path as the bottleneck. At that point you probably want Alloy
rather than a Lambda.

## Why not an off-the-shelf Python Loki library

There is no official Grafana Python SDK for Loki ingest; Grafana's own docs list
only third-party clients and state they cannot support them. Of the community
libraries, the maintained ones are `logging.Handler` integrations built around
Python `LogRecord` objects - designed for shipping an application's own log calls,
not arbitrary records read out of a file - and only one supports structured
metadata at all. None implement retry with backoff or honour `Retry-After`, all
pull in `requests` (about 2.6 MB installed), and the handler-with-background-thread
design fights a Lambda that freezes between invocations.

Hence roughly 250 lines of `urllib` here instead: fewer dependencies, the exact
retry semantics above, and structured metadata support.
