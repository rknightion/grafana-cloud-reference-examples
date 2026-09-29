---
title: Adobe AEM to Loki
description: An AWS Lambda that ships Adobe Experience Manager Cloud Service logs from S3, with all seven log types parsed, into Grafana Cloud Loki. Two dashboards included.
---

# adobe-aem: Adobe Experience Manager Cloud Service logs to Grafana Cloud Loki

An AWS Lambda that picks up the logs Adobe Experience Manager as a Cloud Service forwards into S3,
parses all seven log types, and ships them to Grafana Cloud Loki with every field already
extracted. Two dashboards are included.

Because the fields are parsed once on the way in rather than on every query, you get latency
percentiles by path, CDN cache hit ratio, error rates by Java logger and per-pod health without a
`| json` or `| regexp` stage in any panel.

**Status: alpha. This is a reference implementation, not a supported product.** Read it, deploy it,
adapt it. It is Apache-2.0 licensed and you are expected to change it. Nothing here is covered by a
Grafana support agreement.

Four of the seven parsers are tested against real AEM output; three are built from Adobe's
published format documentation and are unverified against production. See
[The seven log types](#the-seven-log-types).

## What is in the release download

```
README.md              this file
lambda.zip              the function code, ready to deploy
terraform/              Terraform root module, with lambda.zip beside it
  terraform.tfvars.example
cloudformation/          a single standalone template
  template.yaml
  parameters.example.json
dashboards/              import these after the first data arrives
  traffic-and-performance.json
  errors-and-operations.json
MANIFEST.json           version, runtime, and the sha256 of lambda.zip
LICENSE
```

Pick **one** of `terraform/` or `cloudformation/`. They deploy the same thing.

## What you need before you start

1. **AEM log forwarding to S3, already configured in Cloud Manager.** This example reads a bucket;
   it does not set up forwarding. In Cloud Manager that is *Environments -> your environment ->
   Log Forwarding*, with Amazon S3 as the destination. Forward whichever log types you want; all
   seven are handled.

2. **Your Cloud Manager program and environment ids.** They look like `p12345` and `e67890`. If you
   cannot find them in Cloud Manager, read them off any pod name in the logs - the format is
   `cm-p12345-e67890-aem-author-<hash>-<pod>`.

3. **An example object key from the bucket.** You will very likely need to set `key_pattern`, so
   look at a real key first: `aws s3 ls s3://your-aem-log-bucket/ --recursive | head`. See
   [Object keys](#object-keys) for why this is not a fixed value.

4. **A Grafana Cloud Loki endpoint and numeric tenant id.** Both are on your stack's details page:
   Loki -> *Send Logs*. The endpoint looks like `https://logs-prod-012.grafana.net`, the tenant id
   is a number such as `123456`. It is **not** your email address, and **not** the stack id.

5. **A Cloud Access Policy token with the `logs:write` scope**, created under *Access Policies*. A
   Grafana service-account token is a different thing and returns 401 here.

6. **The token stored in AWS Secrets Manager**, in the region and account you are deploying into.
   Neither IaC path accepts the token as an input, so it never lands in Terraform state or a
   CloudFormation event.

7. **Terraform 1.9+**, or nothing beyond the AWS CLI if you use CloudFormation.

See [Grafana Cloud credentials](../grafana-cloud-credentials.md) for how to create and store the
token.

## What this creates in your AWS account

- A Lambda function (Python 3.14, arm64, 1024 MB, 600 s timeout) and its execution role
- A CloudWatch log group with an explicit retention. There is deliberately no never-expire option:
  that is a slow-growing bill nobody notices
- An SQS queue in front of the function, plus a dead-letter queue
- An S3 bucket notification onto that queue
- CloudWatch alarms on function errors and on dead-letter queue depth
- Read access, **for this function's role only**, to the Secrets Manager secret you created above

It does **not** create the bucket (the role gets `s3:GetObject` on your prefix and nothing else,
and never writes or deletes), the secret, or anything in Grafana Cloud. Importing the dashboards is
a manual step.

## Deploy it

### Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars     # bucket, program/env ids, endpoint, tenant, secret
terraform init
terraform apply
```

There is no backend block, on purpose. Add your own `backend.tf`, or run with local state while you
are trying it out.

### CloudFormation

CloudFormation cannot upload function code from your machine, so put `lambda.zip` in an S3 bucket
first. That is the only real difference between the two paths.

```bash
aws s3 cp lambda.zip s3://my-artifacts/adobe-aem/lambda.zip

aws cloudformation deploy \
  --template-file cloudformation/template.yaml \
  --stack-name grafana-cloud-adobe-aem \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      SourceBucketName=acme-aem-logs \
      SourcePrefix=aem/ \
      AemProgramId=p12345 \
      AemEnvId=e67890 \
      AemEnvType=prod \
      GrafanaCloudLokiEndpoint=https://logs-prod-012.grafana.net \
      GrafanaCloudLokiTenantId=123456 \
      CredentialsSecretId=grafana-cloud/loki \
      LambdaCodeS3Bucket=my-artifacts \
      LambdaCodeS3Key=adobe-aem/lambda.zip
```

If the bucket already has a notification configuration you care about, set
`manage_bucket_notification = false` (Terraform) or `EventWiring=Manual` (CloudFormation) and wire
it yourself - the S3 API replaces the **whole** notification configuration on every write. The
stack outputs a `ManualSubscriptionCommand` that merges rather than replaces. Worth checking here,
because whatever set up the forwarding destination may already have put a notification on the
bucket.

### Import the dashboards

Upload `dashboards/*.json` through *Dashboards -> New -> Import*, then set the **Service**, **AEM
environment** and **AEM tier** variables at the top. They are driven by label values, so an
environment that did not set `aem_env_id` offers nothing for that variable, which is why it is
worth setting even with a single environment.

## Check it worked

Do these in order. Each one narrows the problem down.

1. **Wait for AEM to forward, then watch the function run.** Forwarding is on a schedule, not
   instant, so allow up to an hour for the first object. To avoid waiting, copy an AEM log file into
   the bucket yourself.

   ```bash
   aws logs tail /aws/lambda/grafana-cloud-adobe-aem --since 1h --follow
   ```

2. **Confirm the log types and labels.** In *Explore*, pick your Loki data source and run
   `sum by (log_type, aem_tier) (count_over_time({service_name="adobe-aem"}[1h]))`. You should get
   one row per log type you forwarded.

3. **Confirm the fields were parsed.** This is the thing that makes the example worth deploying:
   `sum by (status_class) (count_over_time({service_name="adobe-aem", log_type="aemcdn"}[1h]))`.
   Rows like `2xx`, `4xx`, `5xx` mean parsing is working.

4. **Check the shipper's own health panels.** Open *Adobe AEM - Errors and Operations*. **Unparsed
   lines** should be 0. **Uncorrelated responses** should be small and non-zero. **Log lines/sec by
   type** shows a type dropping to zero, which means forwarding stopped.

5. **Check nothing is stuck.** The dead-letter queue should be empty.

## Configuration

Every setting is an environment variable on the function, with a matching Terraform variable and
CloudFormation parameter.

| Variable | Default | What it does |
| --- | --- | --- |
| `GRAFANA_CLOUD_LOKI_ENDPOINT` | required | Host, base URL or full push URL |
| `GRAFANA_CLOUD_LOKI_TENANT_ID` | required | Numeric Loki tenant id |
| `GRAFANA_CLOUD_CREDENTIALS_SECRET_ID` | required | Secrets Manager id or ARN |
| `SERVICE_NAME` | `adobe-aem` | Value of the `service_name` label |
| `KEY_PATTERN` | see below | Regex with named groups for reading coordinates off an object key |
| `AEM_PROGRAM_ID` | empty | `aem_program_id` label, e.g. `p12345` |
| `AEM_ENV_ID` | empty | `aem_env_id` label, e.g. `e67890` |
| `AEM_ENV_TYPE` | empty | `aem_env_type` label: dev, stage, prod |
| `AEM_TIER` | empty | Fallback tier. A tier found in the key always wins |
| `LINE_CONTENT` | `raw` | `raw` ships the original line; `message` ships only the readable part |
| `SNIFF_CONTENT` | `true` | Identify the log type from the first line when the key does not |
| `CORRELATE_REQUESTS` | `true` | Pair the request log's `->` and `<-` lines |
| `DROP_CLIENT_IP` | `false` | Omit `client_ip` from metadata |
| `LOG_UTC_OFFSET_SECONDS` | `0` | For the two log formats that carry no timezone |
| `MAX_TIMESTAMP_AGE_SECONDS` | `0` (off) | Stamp older lines at ingestion time. Set before a replay |
| `LOKI_STATIC_LABELS` | `{}` | JSON object of extra labels. Low cardinality only |
| `LOG_LEVEL` | `INFO` | The function's own logging, not the data being shipped |

### Labels, and what deliberately is not one

Labels are **deployment coordinates only**: `service_name`, `aem_program_id`, `aem_env_id`,
`aem_env_type`, `aem_tier`, `log_type`, and `level` (only on the four HTTP log types). Everything
the parsers extract - `path`, `status`, `status_class`, `method`, `client_ip`, `duration_ms`,
`cache_status`, `logger`, `exception` and more - goes into **structured metadata** instead. `status`
looks like it should be a label and must not be: it multiplies every other label, and nobody
queries it without also slicing by path or method, which are unbounded.

Querying structured metadata always goes after a `|`, never inside the `{}` selector:

```logql
{log_type="aemcdn", cache_status="HIT"}     # WRONG: empty result, no error
{log_type="aemcdn"} | cache_status="HIT"    # right
```

Read [Loki Ingestion](../loki-ingestion.md) for the full label and structured-metadata rules this
example follows.

## What it costs

**Loki ingest** dominates, and it is per GB, driven by AEM's own volume. On a sampled author-tier
day, `/systemready` and `login.html` health checks were 76% of the access log and 98% of the CDN
log. Filter those out in Cloud Manager's forwarding configuration, not here. `LINE_CONTENT=message`
drops the timestamp and node id from the log line, roughly 40% off an error log. **Lambda** is
charged on GB-seconds; 1024 MB is usually the sweet spot for the regex-per-line parsing this
function does. **CloudWatch Logs** for the function's own output, at `LOG_LEVEL=INFO`. **S3 GET
requests**, one per object, plus SQS requests per notification.

## Troubleshooting

**Everything is in `log_type="unknown"`.** The `KEY_PATTERN` does not match your bucket, and
content sniffing could not identify the format either. Look at a real key and set the pattern.

**`aemaccess` and `aemhttpdaccess` are mixed up.** They are byte-for-byte the same format, so
content alone cannot tell them apart. Only the object key can - make sure your `KEY_PATTERN`
captures `log_type`.

**Nothing arrives, and the function never runs.** Either AEM has not forwarded yet (it is on a
schedule, allow an hour), or the bucket notification is not wired.

**`401` in the function logs.**

| Body | Cause |
| --- | --- |
| `invalid authentication credentials` | Wrong tenant id for that token, or a Grafana service-account token instead of a Cloud Access Policy token |
| `invalid scope requested` | Live token, but its policy lacks `logs:write` |
| `invalid token` | Revoked or malformed |

**`400` with `entry too far behind`.** AEM's own timestamps are older than your tenant's
`reject_old_samples_max_age`, which is a week on Grafana Cloud by default. Set
`MAX_TIMESTAMP_AGE_SECONDS` so old lines are stamped at ingestion instead of failing the batch they
travelled in.

**Latency panels are empty.** They read `duration_ms`, which only exists on the request log's
response lines. Check `aemrequest` is being forwarded and that `CORRELATE_REQUESTS` is on.

**The function times out.** One object is a whole hour or day of logs. Raise `timeout_seconds`
towards the 900 s Lambda maximum, and `memory_mb` with it, since memory buys CPU.

## Limitations

- **Request/response pairing is within one object only.** A request whose response lands in the
  next hour's file is never paired, and ships with `correlated="false"`.
- **Three parsers are unverified against production output** - see
  [The seven log types](#the-seven-log-types).
- **The S3 key layout is a guess until you configure it.** Adobe publishes no single layout.
- One Loki tenant per deployed function. Two tenants means two deployments.
- Objects are never deleted or moved after processing.
- The dashboards are Grafana schema v2. A much older Grafana may not accept them without
  conversion.

## Object keys

Adobe does not document a single S3 key layout for forwarded logs. So the layout is a configurable
regex, `KEY_PATTERN`, with named groups drawn from `program_id`, `env_id`, `env_type`, `tier`,
`log_type`. The default matches Adobe's own download naming -
`author_aemaccess_2026-07-24.log` - and is *searched* rather than anchored, so any prefix in front
of it is ignored. For a bucket laid out by program and environment:

```hcl
key_pattern = "p(?P<program_id>[0-9]+)/e(?P<env_id>[0-9]+)/(?P<tier>author|publish|preview)/(?P<log_type>aem[a-z]+)/"
```

A pattern that captures none of the recognised groups is rejected at cold start. If the pattern
finds no log type, the first line's shape is used instead, so a mismatched pattern degrades rather
than breaking.

## The seven log types

| `log_type` | Tier | Verified against | Notes |
| --- | --- | --- | --- |
| `aemaccess` | author, publish, preview | real output | Apache-like, node id first |
| `aemerror` | author, publish, preview | real output | Extracts the Java exception, including from stack-trace lines |
| `aemrequest` | author, publish, preview | real output | Paired `->`/`<-` lines |
| `aemcdn` | author, publish, preview | real output | JSON Lines; fields renamed so one panel spans CDN and access |
| `aemdispatcher` | publish | Adobe docs only | Trailing fields vary by dispatcher build |
| `aemhttpdaccess` | publish | Adobe docs only | Same format as `aemaccess`; only the key distinguishes them |
| `aemhttpderror` | publish | Adobe docs only | Apache error log |

The three marked *Adobe docs only* are built from the published format strings and tested against
hand-written fixtures, because no real sample was available. **No line is ever dropped.** A line no
parser recognises ships as raw text with `parse_status="unmatched"`.

## How it works

S3 notification -> SQS -> Lambda -> parse -> Loki push API.

**Why the fields are parsed on the way in.** A raw shipper puts the whole line in Loki and leaves
every query to re-parse it. Parsing once at write time moves that cost to one place and makes the
fields aggregatable directly.

**Why the request log is correlated.** AEM splits one HTTP request across two lines: the first
carries the method and path, the second carries the status and the duration. Loki has no join, so
no query can put them back together. Pairing them on ingest is what makes latency-by-path possible
at all.

**Why SQS rather than invoking from S3 directly.** The queue gives batching, a visibility timeout
you control, a retry policy, a dead-letter queue you can inspect, and partial batch failure so one
unreadable object does not force redelivery of the whole batch.

**Why the Loki push API rather than OTLP.** See [OTLP vs Loki Push](../otlp-vs-loki-push.md).

## More detail

- [Loki label and batching rules](../loki-ingestion.md)
- [Getting Grafana Cloud credentials](../grafana-cloud-credentials.md)
- [OTLP versus the Loki push API](../otlp-vs-loki-push.md)
- [Generic S3 to Loki, the format-agnostic version of this example](generic-s3.md)
- [Test fixtures and their provenance](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/examples/adobe-aem/fixtures/README.md)
- [Adobe: log forwarding](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/log-forwarding)
- [Adobe: logging](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/logging)

The full README, including the working-on-this-example commands, ships inside the release zip and
is also on
[GitHub](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/examples/adobe-aem/README.md).
