# generic-s3: S3 files to Grafana Cloud Loki

An AWS Lambda that ships files landing in an S3 bucket to Grafana Cloud Loki.
Format-agnostic: plain text, JSON Lines, a JSON array or CSV, each optionally
gzipped. Objects are streamed and decompressed on the fly, so a multi-gigabyte
gzipped export runs in a 512 MB function.

Status: **alpha**. Read it, deploy it, adapt it. It is a reference
implementation, not a supported product.

## What gets created

- A Lambda function (Python 3.14, arm64) with its execution role
- A CloudWatch log group with an explicit retention (never "never expire")
- An SQS queue in front of the function, plus a dead-letter queue
- An S3 bucket notification onto that queue
- CloudWatch alarms on function errors and on DLQ depth
- Read access, for the function's role only, to a Secrets Manager secret **you
  created beforehand**

Two things it deliberately does not create. The **bucket** is an input; it never
modifies or deletes your objects, and the role is granted `s3:GetObject` and
nothing else. The **secret** is also an input: neither IaC path accepts the token,
so the token never lands in Terraform state or in a CloudFormation event.

### Why SQS rather than invoking the function directly from S3

A direct bucket notification gives one invocation per object, no batching, two
retries and then silence. The queue gives batching, a visibility timeout you
control, a retry policy, a dead-letter queue you can inspect, and partial batch
failure so one unreadable object does not force the redelivery of every other
object in the batch. Set `use_sqs = false` if you want the simple path anyway.

## Deploying

### Before you start

You need a Grafana Cloud Loki endpoint, a numeric tenant id, and a Cloud Access
Policy token scoped to `logs:write`. See
[`docs/grafana-cloud-credentials.md`](../../docs/grafana-cloud-credentials.md) for
where each of those comes from.

Put the token in Secrets Manager yourself, before deploying. Neither the
Terraform nor the CloudFormation takes the token as an input, so it never lands
in state, in a plan output, or in a CloudFormation event.

Write it to a file rather than passing it inline, so the token does not land in
your shell history or in the process list:

```bash
umask 077 && cat > /tmp/gc-loki.json <<'JSON'
{"tenant_id":"123456","token":"glc_eyJ..."}
JSON
aws secretsmanager create-secret \
  --name grafana-cloud/loki \
  --secret-string file:///tmp/gc-loki.json
rm -f /tmp/gc-loki.json
```

### Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform apply
```

There is no backend block. Add your own `backend.tf`, or run it with local state
if you are just trying it out.

### CloudFormation

```bash
aws cloudformation deploy \
  --template-file cloudformation/template.yaml \
  --stack-name grafana-cloud-generic-s3 \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      SourceBucketName=acme-logs \
      GrafanaCloudLokiEndpoint=https://logs-prod-012.grafana.net \
      GrafanaCloudLokiTenantId=123456 \
      CredentialsSecretId=grafana-cloud/loki \
      LambdaCodeS3Bucket=my-artifacts \
      LambdaCodeS3Key=generic-s3/lambda.zip
```

CloudFormation cannot upload function code from your machine, so the zip has to
be in an S3 bucket first. The Terraform path uploads it for you. That is the only
real difference between the two.

## Configuration

Both IaC paths set these on the function; you can also set them by hand.

| Variable | Default | What it does |
| --- | --- | --- |
| `GRAFANA_CLOUD_LOKI_ENDPOINT` | required | Host, base URL or full push URL. All three forms are accepted. |
| `GRAFANA_CLOUD_LOKI_TENANT_ID` | required | Numeric tenant/instance id. Not your email. |
| `GRAFANA_CLOUD_CREDENTIALS_SECRET_ID` | required | Secrets Manager id or ARN. |
| `SERVICE_NAME` | `generic-s3` | Value of the `service_name` label. |
| `RECORD_FORMAT` | `auto` | `auto`, `lines`, `jsonl`, `json_array`, `csv`. |
| `PREFIX_LABEL_DEPTH` | `0` | Leading key segments to expose as a `prefix` label. |
| `TIMESTAMP_FIELD` | unset | JSON field to read the event time from. Unset means ingestion time. |
| `TIMESTAMP_FORMAT` | `rfc3339` | `rfc3339`, `epoch_s`, `epoch_ms`, `epoch_us`, `epoch_ns`, or a `strptime` pattern. |
| `MAX_TIMESTAMP_AGE_SECONDS` | `0` (off) | Fall back to ingestion time for anything older, instead of being rejected. |
| `LOKI_STATIC_LABELS` | `{}` | JSON object of extra labels, e.g. `{"env":"prod"}`. |
| `LOKI_BATCH_MAX_LINES` | `5000` | Lines per push. |
| `LOKI_BATCH_MAX_BYTES` | `4194304` | Approximate bytes per push. |
| `LOKI_COMPRESS` | `true` | gzip the push body. |
| `LOKI_MAX_RETRIES` | `5` | Attempts per batch before giving up. |
| `LOG_LEVEL` | `INFO` | The function's own logging, not the data being shipped. |

### Labels, and what deliberately is not one

Streams are labelled `service_name` and `bucket`, plus anything in
`LOKI_STATIC_LABELS`, plus `prefix` if `PREFIX_LABEL_DEPTH` is set.

The object key, the record number and the object version go into **structured
metadata** on each line, not into labels. A label derived from an object key is
one Loki stream per file, which is a cost and query-performance incident. The
client rejects such a label rather than letting you find out from your bill;
see [`docs/loki-ingestion.md`](../../docs/loki-ingestion.md).

Query them like this:

```logql
{service_name="generic-s3", bucket="acme-logs"} | object_key=~"logs/2026-09-.*"
```

### Timestamps

By default every line is stamped with the time it was read, not a time parsed out
of the data. That is the safe default: Loki rejects samples older than the
tenant's `reject_old_samples_max_age` (a week on Grafana Cloud unless you have
had it raised) with a 400 that no retry fixes.

Set `TIMESTAMP_FIELD` when the records carry their own event time and you want
it. For a backfill older than the window, either get the limit raised on your
tenant first or leave extraction off and accept ingestion-time ordering.

## Costs worth knowing before you turn it on

- **Loki ingest** is the dominant cost and it is per GB. This ships the whole file.
  If you only want a subset, filter in `handler.py` before the entry is yielded -
  dropping lines at the source is free, dropping them at query time is not.
- **Lambda** is charged on GB-seconds. Streaming keeps memory flat, so raise
  memory only if you see the function running out; more memory also means more CPU
  and therefore faster gzip.
- **CloudWatch Logs** for the function's own output. `LOG_LEVEL=DEBUG` emits a
  line per batch; leave it at `INFO` outside an investigation.
- **S3 GET requests**, one per object.

## Operating it

The alarms cover the two failure modes that matter: the function erroring, and
messages piling up in the dead-letter queue. A message in the DLQ carries the
original S3 notification, so redriving it after a fix reprocesses exactly the
objects that failed.

Duplicate lines after a failure are expected and harmless. Loki deduplicates
entries that are identical in timestamp, labels and line content within a stream,
and a redelivered batch produces exactly that.

## Limitations

- A single JSON array is read whole, bounded by `max_json_bytes` (64 MB). Loki
  wants JSON Lines; a streaming array parser needs a dependency the core does not
  have.
- `.zip` and `.bz2` objects are not handled. Zip needs random access, which a
  streaming read cannot give.
- One Loki tenant per deployed function. Two tenants means two deployments.
- Objects are never deleted or moved after processing. If you need
  process-then-archive, add an S3 lifecycle rule on the prefix; doing it in the
  function makes the retry semantics much harder to reason about.

## Development

```bash
just test                 # from the repo root
just package generic-s3   # builds dist/generic-s3-<version>.zip
```
