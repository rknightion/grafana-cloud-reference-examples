# generic-s3: S3 files to Grafana Cloud Loki

An AWS Lambda that ships files landing in an S3 bucket to Grafana Cloud Loki.
Format-agnostic: plain text, JSON Lines, a JSON array or CSV, each optionally
gzipped. Objects are streamed and decompressed on the fly, so a multi-gigabyte
gzipped export runs in a 512 MB function.

**Status: alpha. This is a reference implementation, not a supported product.**
Read it, deploy it, adapt it. It is Apache-2.0 licensed and you are expected to
change it. Nothing here is covered by a Grafana support agreement.

## What is in this download

```
README.md              this file
lambda.zip             the function code, ready to deploy
terraform/             Terraform root module, with lambda.zip beside it
  terraform.tfvars.example
cloudformation/        a single standalone template
  template.yaml
  parameters.example.json
MANIFEST.json          version, runtime, and the sha256 of lambda.zip
LICENSE
```

Pick **one** of `terraform/` or `cloudformation/`. They deploy the same thing.

## What you need before you start

1. **An S3 bucket** that something is already writing files into. This example
   never creates, modifies or deletes your objects.
2. **A Grafana Cloud Loki endpoint and numeric tenant id.** Both are on your
   stack's details page: Loki → *Send Logs*. The endpoint looks like
   `https://logs-prod-012.grafana.net` and the tenant id is a number such as
   `123456`. It is **not** your email address, and it is **not** the stack id.
3. **A Cloud Access Policy token with the `logs:write` scope.** Create it under
   *Access Policies* in Grafana Cloud, not as a Grafana service-account token -
   the two are not interchangeable and a service-account token returns 401 here.
4. **The token stored in AWS Secrets Manager**, in the same region and account
   you are deploying into. Neither IaC path accepts the token as an input, so it
   never lands in Terraform state or a CloudFormation event.

   Write it from a file rather than inline, so it stays out of your shell history
   and the process list:

   ```bash
   umask 077 && cat > /tmp/gc-loki.json <<'JSON'
   {"tenant_id":"123456","token":"glc_eyJ..."}
   JSON
   aws secretsmanager create-secret \
     --name grafana-cloud/loki \
     --secret-string file:///tmp/gc-loki.json
   rm -f /tmp/gc-loki.json
   ```

   A bare token string works too; the JSON form is preferred because it keeps the
   tenant id with the credential it belongs to.

5. **Terraform 1.9+**, or nothing beyond the AWS CLI if you use CloudFormation.

## What this creates in your AWS account

- A Lambda function (Python 3.14, arm64) and its execution role
- A CloudWatch log group with an explicit retention. There is deliberately no
  never-expire option: that is a slow-growing bill nobody notices
- An SQS queue in front of the function, plus a dead-letter queue
- An S3 bucket notification onto that queue
- CloudWatch alarms on function errors and on dead-letter queue depth
- Read access, **for this function's role only**, to the Secrets Manager secret
  you created above

Two things it deliberately does not create. The **bucket** is an input: the role
gets `s3:GetObject` on your chosen prefix and nothing else. The **secret** is an
input for the reason given above.

Rough steady-state AWS cost for a few thousand objects a day is cents; see
[What it costs](#what-it-costs).

## Deploy it

### Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars          # bucket, endpoint, tenant id, secret name
terraform init
terraform apply
```

There is no backend block, on purpose. Add your own `backend.tf`, or run with
local state while you are trying it out.

### CloudFormation

CloudFormation cannot upload function code from your machine, so put `lambda.zip`
in an S3 bucket first. That is the only real difference between the two paths.

```bash
aws s3 cp lambda.zip s3://my-artifacts/generic-s3/lambda.zip

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

If the bucket already has a notification configuration you care about, set
`manage_bucket_notification = false` (Terraform) or `EventWiring=Manual`
(CloudFormation) and wire it yourself - the S3 API replaces the **whole**
notification configuration on every write. The CloudFormation stack outputs a
`ManualSubscriptionCommand` that merges rather than replaces.

## Check it worked

Do these in order. Each one narrows the problem down.

**1. Drop a file in and watch the function run.**

```bash
printf 'hello from generic-s3\n' > /tmp/probe.log
aws s3 cp /tmp/probe.log s3://acme-logs/app-logs/probe.log

# Terraform prints the name; for CloudFormation it is the stack name.
aws logs tail /aws/lambda/grafana-cloud-generic-s3 --since 5m --follow
```

A successful invocation logs one JSON line per object:

```json
{"level":"INFO","message":"object shipped","uri":"s3://acme-logs/app-logs/probe.log","lines":1}
```

**2. Query it back in Grafana.** In *Explore*, pick your Loki data source and run:

```logql
{service_name="generic-s3"}
```

The Terraform path prints this query as the `loki_query` output, and the
CloudFormation stack as the `LokiQuery` output, already filled in with your
`service_name`.

**3. Confirm the parsed fields arrived.** Expand any line in Explore. You should
see `object_key`, `bucket` and `record` under the line, as structured metadata
rather than as part of the text.

**4. Check nothing is stuck.** The dead-letter queue should be empty:

```bash
# Terraform
DLQ_URL=$(terraform -chdir=terraform output -raw dlq_url)

# CloudFormation
DLQ_URL=$(aws cloudformation describe-stacks --stack-name grafana-cloud-generic-s3 \
  --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text)

aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages
```

## Configuration

Both IaC paths set these on the function. Every one has a matching Terraform
variable and CloudFormation parameter, so prefer changing it there rather than
editing the function by hand.

| Variable | Default | What it does |
| --- | --- | --- |
| `GRAFANA_CLOUD_LOKI_ENDPOINT` | required | Host, base URL or full push URL. All three forms are accepted. |
| `GRAFANA_CLOUD_LOKI_TENANT_ID` | required | Numeric tenant/instance id. Not your email. |
| `GRAFANA_CLOUD_CREDENTIALS_SECRET_ID` | required | Secrets Manager id or ARN. |
| `SERVICE_NAME` | `generic-s3` | Value of the `service_name` label. |
| `RECORD_FORMAT` | `auto` | `auto`, `lines`, `jsonl`, `json_array`, `csv`. |
| `SOURCE_KEY_SUFFIX` | unset | Only process keys with this suffix. |
| `PREFIX_LABEL_DEPTH` | `0` | Leading key segments to expose as a `prefix` label. |
| `TIMESTAMP_FIELD` | unset | JSON field to read the event time from. Unset means ingestion time. |
| `TIMESTAMP_FORMAT` | `rfc3339` | `rfc3339`, `epoch_s`, `epoch_ms`, `epoch_us`, `epoch_ns`, or a `strptime` pattern. |
| `MAX_TIMESTAMP_AGE_SECONDS` | `0` (off) | Fall back to ingestion time for anything older, instead of having it rejected. |
| `LOKI_STATIC_LABELS` | `{}` | JSON object of extra labels, e.g. `{"env":"prod"}`. Low cardinality only. |
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
client rejects such a label rather than letting you find out from your bill.

Structured metadata is filtered **after a `|`**, never inside the `{}` selector:

```logql
{service_name="generic-s3", bucket="acme-logs"} | object_key=~"logs/2026-09-.*"
```

Putting `object_key` inside the braces returns an empty result with no error,
because `{}` matches labels only. That is the single most common reason a query
against this data looks broken.

### Timestamps

By default every line is stamped with the time it was read, not a time parsed out
of the data. That is the safe default: Loki rejects samples older than the
tenant's `reject_old_samples_max_age` (a week on Grafana Cloud unless you have
had it raised) with a 400 that no retry fixes.

Set `TIMESTAMP_FIELD` when the records carry their own event time and you want
it. For a backfill older than that window, either get the limit raised on your
tenant first, or set `MAX_TIMESTAMP_AGE_SECONDS` so old lines fall back to
ingestion time instead of failing the batch they travelled in.

## What it costs

- **Loki ingest** dominates, and it is per GB. This ships the whole file. If you
  only want a subset, filter in `handler.py` before the entry is yielded -
  dropping lines at the source is free, dropping them at query time is not.
- **Lambda** is charged on GB-seconds. Streaming keeps memory flat, so raise
  memory only if the function runs out; more memory also means more CPU and so
  faster gzip.
- **CloudWatch Logs** for the function's own output. `LOG_LEVEL=DEBUG` emits a
  line per batch; leave it at `INFO` outside an investigation.
- **S3 GET requests**, one per object, plus SQS requests per notification.

## Troubleshooting

**Nothing arrives in Loki, and the function never runs.** The bucket
notification is not wired. Check it:

```bash
aws s3api get-bucket-notification-configuration --bucket acme-logs
```

If it is empty, something else overwrote it - the S3 API replaces the whole
configuration on every write. Re-apply, or use the `ManualSubscriptionCommand`
output, which merges.

**`401` in the function logs.** Three different causes, and the body tells you
which:

| Body | Cause |
| --- | --- |
| `invalid authentication credentials` | Wrong tenant id for that token, or a Grafana service-account token instead of a Cloud Access Policy token. |
| `invalid scope requested` | Live token, but its policy lacks `logs:write`. |
| `invalid token` | Revoked or malformed. |

**`400` with `entry too far behind`.** The data's own timestamps are older than
your tenant's `reject_old_samples_max_age`. Set `MAX_TIMESTAMP_AGE_SECONDS`, or
clear `TIMESTAMP_FIELD` so lines are stamped at ingestion, or ask Grafana support
to raise the limit for a one-off backfill.

**`429` in the logs, and things are slow.** You are hitting your tenant's rate
limit, usually on a first run against a bucket with a large backlog. Set
`reserved_concurrency` to a real number (start with 10) so the fan-out is bounded.

**The query returns nothing but the function says it shipped lines.** Almost
always the `{}`-versus-`|` mistake above. Try the bare `{service_name="..."}`
selector with no pipeline at all, over *Last 6 hours*.

**AccessDenied on GetObject.** The role is scoped to `source_prefix`. If you
changed the prefix after applying, re-apply so the policy follows.

**Objects processed twice.** Expected after a retry, and harmless: Loki
deduplicates entries identical in timestamp, labels and line content within a
stream, and a redelivered batch produces exactly that.

**Something is in the dead-letter queue.** The message carries the original S3
notification, so redriving it after a fix reprocesses exactly the objects that
failed:

```bash
# Terraform
DLQ_ARN=$(terraform -chdir=terraform output -raw dlq_arn)

# CloudFormation
DLQ_ARN=$(aws cloudformation describe-stacks --stack-name grafana-cloud-generic-s3 \
  --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueArn'].OutputValue" --output text)

# The ARN, not the URL - start-message-move-task will not take a URL.
aws sqs start-message-move-task --source-arn "$DLQ_ARN"
```

## Limitations

- A single JSON array is read whole, bounded at 64 MB. Loki wants JSON Lines; a
  streaming array parser needs a dependency the core deliberately does not have.
- `.zip` and `.bz2` are not handled. Zip needs random access, which a streaming
  read cannot give.
- One Loki tenant per deployed function. Two tenants means two deployments.
- Objects are never deleted or moved after processing. If you need
  process-then-archive, add an S3 lifecycle rule on the prefix; doing it in the
  function makes the retry semantics much harder to reason about.
- No ordering guarantee between objects. Within an object, lines keep their order.

## How it works

S3 notification → SQS → Lambda → Loki push API.

**Why SQS rather than invoking the function directly from S3.** A direct
notification gives one invocation per object, no batching, two retries and then
silence. The queue gives batching, a visibility timeout you control, a retry
policy, a dead-letter queue you can inspect, and partial batch failure so one
unreadable object does not force redelivery of every other object in the batch.
Set `use_sqs = false` if you want the simple path anyway.

**Why the Loki push API rather than OTLP.** For arbitrary third-party records,
OTLP's resource-attribute promotion model indexes the wrong things: only specific
resource attributes become labels without per-stack configuration, and a log
file's useful dimensions are not resource attributes.

## More detail

These live in the public repository rather than in this download:

- [Loki label and batching rules](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/loki-ingestion.md)
- [Getting Grafana Cloud credentials](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/grafana-cloud-credentials.md)
- [OTLP versus the Loki push API](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/otlp-vs-loki-push.md)
- [Lambda runtime lifecycle](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/runtimes.md)

Working on this example from a clone:

```bash
just test                 # from the repo root
just package generic-s3   # builds dist/generic-s3-<version>.zip
```
