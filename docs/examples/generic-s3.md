---
title: Generic S3 to Loki
description: An AWS Lambda that ships files landing in an S3 bucket to Grafana Cloud Loki. Format-agnostic - text, JSON Lines, JSON array or CSV, optionally gzipped.
---

# generic-s3: S3 files to Grafana Cloud Loki

An AWS Lambda that ships files landing in an S3 bucket to Grafana Cloud Loki. Format-agnostic:
plain text, JSON Lines, a JSON array or CSV, each optionally gzipped. Objects are streamed and
decompressed on the fly, so a multi-gigabyte gzipped export runs in a 512 MB function.

**Status: alpha. This is a reference implementation, not a supported product.** Read it, deploy it,
adapt it. It is Apache-2.0 licensed and you are expected to change it. Nothing here is covered by a
Grafana support agreement.

## What is in the release download

```
README.md              this file
lambda.zip              the function code, ready to deploy
terraform/              Terraform root module, with lambda.zip beside it
  terraform.tfvars.example
cloudformation/          a single standalone template
  template.yaml
  parameters.example.json
MANIFEST.json           version, runtime, and the sha256 of lambda.zip
LICENSE
```

Pick **one** of `terraform/` or `cloudformation/`. They deploy the same thing.

## What you need before you start

1. **An S3 bucket** that something is already writing files into. This example never creates,
   modifies or deletes your objects.
2. **A Grafana Cloud Loki endpoint and numeric tenant id.** Both are on your stack's details page:
   Loki -> *Send Logs*. The endpoint looks like `https://logs-prod-012.grafana.net` and the tenant
   id is a number such as `123456`. It is **not** your email address, and it is **not** the stack
   id.
3. **A Cloud Access Policy token with the `logs:write` scope.** Create it under *Access Policies*
   in Grafana Cloud, not as a Grafana service-account token - the two are not interchangeable and a
   service-account token returns 401 here.
4. **The token stored in AWS Secrets Manager**, in the same region and account you are deploying
   into. Neither IaC path accepts the token as an input, so it never lands in Terraform state or a
   CloudFormation event.
5. **Terraform 1.9+**, or nothing beyond the AWS CLI if you use CloudFormation.

See [Grafana Cloud credentials](../grafana-cloud-credentials.md) for how to create and store the
token.

## What this creates in your AWS account

- A Lambda function (Python 3.14, arm64) and its execution role
- A CloudWatch log group with an explicit retention. There is deliberately no never-expire option:
  that is a slow-growing bill nobody notices
- An SQS queue in front of the function, plus a dead-letter queue
- An S3 bucket notification onto that queue
- CloudWatch alarms on function errors and on dead-letter queue depth
- Read access, **for this function's role only**, to the Secrets Manager secret you created above

Two things it deliberately does not create. The **bucket** is an input: the role gets
`s3:GetObject` on your chosen prefix and nothing else. The **secret** is an input for the reason
given above.

Rough steady-state AWS cost for a few thousand objects a day is cents; see
[What it costs](#what-it-costs).

## Deploy it

### Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars          # bucket, endpoint, tenant id, secret
terraform init
terraform apply
```

There is no backend block, on purpose. Add your own `backend.tf`, or run with local state while you
are trying it out.

### CloudFormation

CloudFormation cannot upload function code from your machine, so put `lambda.zip` in an S3 bucket
first. That is the only real difference between the two paths.

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
`manage_bucket_notification = false` (Terraform) or `EventWiring=Manual` (CloudFormation) and wire
it yourself - the S3 API replaces the **whole** notification configuration on every write. The
CloudFormation stack outputs a `ManualSubscriptionCommand` that merges rather than replaces.

## Check it worked

Do these in order. Each one narrows the problem down.

1. **Drop a file in and watch the function run.**

   ```bash
   printf 'hello from generic-s3\n' > /tmp/probe.log
   aws s3 cp /tmp/probe.log s3://acme-logs/app-logs/probe.log
   aws logs tail /aws/lambda/grafana-cloud-generic-s3 --since 5m --follow
   ```

2. **Query it back in Grafana.** In *Explore*, pick your Loki data source and run
   `{service_name="generic-s3"}`.

3. **Confirm the parsed fields arrived.** Expand any line in Explore. You should see `object_key`,
   `bucket` and `record` under the line, as structured metadata rather than as part of the text.

4. **Check nothing is stuck.** The dead-letter queue should be empty.

## Configuration

Both IaC paths set these on the function, each with a matching Terraform variable and
CloudFormation parameter.

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
| `LOKI_COMPRESS` | `true` | gzip the push body. |
| `LOG_LEVEL` | `INFO` | The function's own logging, not the data being shipped. |
| `LOG_DEBUG_SAMPLE_RATE` | `0` | Fraction of invocations, 0 to 1, that log at DEBUG whatever `LOG_LEVEL` says. `0.05` gives DEBUG detail to diagnose with while paying for it on one invocation in twenty. |

### Labels, and what deliberately is not one

Streams are labelled `service_name` and `bucket`, plus anything in `LOKI_STATIC_LABELS`, plus
`prefix` if `PREFIX_LABEL_DEPTH` is set. The object key, the record number and the object version
go into **structured metadata** on each line, not into labels: a label derived from an object key
is one Loki stream per file, which is a cost and query-performance incident. The client rejects
such a label rather than letting you find out from your bill.

Structured metadata is filtered **after a `|`**, never inside the `{}` selector:

```logql
{service_name="generic-s3", bucket="acme-logs"} | object_key=~"logs/2026-09-.*"
```

Read [Loki Ingestion](../loki-ingestion.md) for the full label and structured-metadata rules this
example follows.

### Timestamps

By default every line is stamped with the time it was read, not a time parsed out of the data.
Loki rejects samples older than the tenant's `reject_old_samples_max_age` (a week on Grafana Cloud
unless you have had it raised) with a 400 that no retry fixes. Set `TIMESTAMP_FIELD` when the
records carry their own event time and you want it, and `MAX_TIMESTAMP_AGE_SECONDS` so old lines
fall back to ingestion time instead of failing the batch they travelled in.

## What it costs

**Loki ingest** dominates, and it is per GB. This ships the whole file; filter in `handler.py`
before the entry is yielded if you only want a subset, since dropping lines at the source is free
and dropping them at query time is not. **Lambda** is charged on GB-seconds; streaming keeps memory
flat. **CloudWatch Logs** for the function's own output. **S3 GET requests**, one per object, plus
SQS requests per notification.

## Troubleshooting

**Nothing arrives in Loki, and the function never runs.** The bucket notification is not wired.
Check with `aws s3api get-bucket-notification-configuration --bucket acme-logs`. If it is empty,
something else overwrote it - the S3 API replaces the whole configuration on every write.

**`401` in the function logs.**

| Body | Cause |
| --- | --- |
| `invalid authentication credentials` | Wrong tenant id for that token, or a Grafana service-account token instead of a Cloud Access Policy token. |
| `invalid scope requested` | Live token, but its policy lacks `logs:write`. |
| `invalid token` | Revoked or malformed. |

**`400` with `entry too far behind`.** The data's own timestamps are older than your tenant's
`reject_old_samples_max_age`. Set `MAX_TIMESTAMP_AGE_SECONDS`, clear `TIMESTAMP_FIELD` so lines are
stamped at ingestion, or ask Grafana support to raise the limit for a one-off backfill.

**`429` in the logs, and things are slow.** You are hitting your tenant's rate limit. Set
`reserved_concurrency` to a real number (start with 10) so the fan-out is bounded.

**The query returns nothing but the function says it shipped lines.** Almost always the
`{}`-versus-`|` mistake above. Try the bare `{service_name="..."}` selector with no pipeline at
all, over *Last 6 hours*.

**AccessDenied on GetObject.** The role is scoped to `source_prefix`. If you changed the prefix
after applying, re-apply so the policy follows.

## Limitations

- A single JSON array is read whole, bounded at 64 MB. Loki wants JSON Lines; a streaming array
  parser needs a dependency the core deliberately does not have.
- `.zip` and `.bz2` are not handled. Zip needs random access, which a streaming read cannot give.
- One Loki tenant per deployed function. Two tenants means two deployments.
- Objects are never deleted or moved after processing.
- No ordering guarantee between objects. Within an object, lines keep their order.

## How it works

S3 notification -> SQS -> Lambda -> Loki push API.

**Why SQS rather than invoking the function directly from S3.** The queue gives batching, a
visibility timeout you control, a retry policy, a dead-letter queue you can inspect, and partial
batch failure so one unreadable object does not force redelivery of every other object in the
batch. Set `use_sqs = false` if you want the simple path anyway.

**Why the Loki push API rather than OTLP.** See [OTLP vs Loki Push](../otlp-vs-loki-push.md).

## More detail

- [Loki label and batching rules](../loki-ingestion.md)
- [Getting Grafana Cloud credentials](../grafana-cloud-credentials.md)
- [OTLP versus the Loki push API](../otlp-vs-loki-push.md)
- [Lambda runtime lifecycle](../runtimes.md)
- [Adobe AEM to Loki, the AEM-specific version of this example](adobe-aem.md)

The full README, including the working-on-this-example commands, ships inside the release zip and
is also on
[GitHub](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/examples/generic-s3/README.md).
