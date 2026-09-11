# adobe-aem: Adobe Experience Manager Cloud Service logs to Grafana Cloud Loki

An AWS Lambda that picks up the logs Adobe Experience Manager as a Cloud Service
forwards into S3, parses all seven log types, and ships them to Grafana Cloud
Loki with every field already extracted. Two dashboards are included.

Because the fields are parsed once on the way in rather than on every query, you
get latency percentiles by path, CDN cache hit ratio, error rates by Java logger
and per-pod health without a `| json` or `| regexp` stage in any panel.

**Status: alpha. This is a reference implementation, not a supported product.**
Read it, deploy it, adapt it. It is Apache-2.0 licensed and you are expected to
change it. Nothing here is covered by a Grafana support agreement.

Four of the seven parsers are tested against real AEM output; three are built
from Adobe's published format documentation and are unverified against
production. See [The seven log types](#the-seven-log-types).

## What is in this download

```
README.md              this file
lambda.zip             the function code, ready to deploy
terraform/             Terraform root module, with lambda.zip beside it
  terraform.tfvars.example
cloudformation/        a single standalone template
  template.yaml
  parameters.example.json
dashboards/            import these after the first data arrives
  traffic-and-performance.json
  errors-and-operations.json
MANIFEST.json          version, runtime, and the sha256 of lambda.zip
LICENSE
```

Pick **one** of `terraform/` or `cloudformation/`. They deploy the same thing.

## What you need before you start

1. **AEM log forwarding to S3, already configured in Cloud Manager.** This
   example reads a bucket; it does not set up forwarding. In Cloud Manager that
   is *Environments → your environment → Log Forwarding*, with Amazon S3 as the
   destination. Forward whichever log types you want; all seven are handled.

2. **Your Cloud Manager program and environment ids.** They look like `p12345`
   and `e67890`. If you cannot find them in Cloud Manager, read them off any pod
   name in the logs - the format is `cm-p12345-e67890-aem-author-<hash>-<pod>`.

3. **An example object key from the bucket.** You will very likely need to set
   `key_pattern`, so look at a real key first:

   ```bash
   aws s3 ls s3://your-aem-log-bucket/ --recursive | head
   ```

   See [Object keys](#object-keys) for why this is not a fixed value.

4. **A Grafana Cloud Loki endpoint and numeric tenant id.** Both are on your
   stack's details page: Loki → *Send Logs*. The endpoint looks like
   `https://logs-prod-012.grafana.net`, the tenant id is a number such as
   `123456`. It is **not** your email address, and **not** the stack id.

5. **A Cloud Access Policy token with the `logs:write` scope**, created under
   *Access Policies*. A Grafana service-account token is a different thing and
   returns 401 here.

6. **The token stored in AWS Secrets Manager**, in the region and account you
   are deploying into. Neither IaC path accepts the token as an input, so it
   never lands in Terraform state or a CloudFormation event.

   ```bash
   umask 077 && cat > /tmp/gc-loki.json <<'JSON'
   {"tenant_id":"123456","token":"glc_eyJ..."}
   JSON
   aws secretsmanager create-secret \
     --name grafana-cloud/loki \
     --secret-string file:///tmp/gc-loki.json
   rm -f /tmp/gc-loki.json
   ```

7. **Terraform 1.9+**, or nothing beyond the AWS CLI if you use CloudFormation.

## What this creates in your AWS account

- A Lambda function (Python 3.14, arm64, 1024 MB, 600 s timeout) and its
  execution role
- A CloudWatch log group with an explicit retention. There is deliberately no
  never-expire option: that is a slow-growing bill nobody notices
- An SQS queue in front of the function, plus a dead-letter queue
- An S3 bucket notification onto that queue
- CloudWatch alarms on function errors and on dead-letter queue depth
- Read access, **for this function's role only**, to the Secrets Manager secret
  you created above

It does **not** create the bucket (the role gets `s3:GetObject` on your prefix
and nothing else, and never writes or deletes), the secret, or anything in
Grafana Cloud. Importing the dashboards is a manual step.

The memory and timeout are higher than a plain log shipper because every line is
parsed with a regex rather than passed through. AEM forwards on a schedule, so
one object is a whole hour or day of one log type from one tier - a sampled
author-tier day held 46,000 request-log lines in 5.6 MB, and a busy publish tier
is an order of magnitude more.

## Deploy it

### Terraform

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
$EDITOR terraform.tfvars     # bucket, program/env ids, endpoint, tenant, secret
terraform init
terraform apply
```

There is no backend block, on purpose. Add your own `backend.tf`, or run with
local state while you are trying it out.

### CloudFormation

CloudFormation cannot upload function code from your machine, so put `lambda.zip`
in an S3 bucket first. That is the only real difference between the two paths.

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
`manage_bucket_notification = false` (Terraform) or `EventWiring=Manual`
(CloudFormation) and wire it yourself - the S3 API replaces the **whole**
notification configuration on every write. The stack outputs a
`ManualSubscriptionCommand` that merges rather than replaces. Worth checking
here, because whatever set up the forwarding destination may already have put a
notification on the bucket.

### Import the dashboards

Do this after step 2 of the next section, so the variables have values to offer.

Upload `dashboards/*.json` through *Dashboards → New → Import*, or:

```bash
curl -X POST \
  -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://<your-stack>.grafana.net/apis/dashboard.grafana.app/v2/namespaces/stacks-<stack-id>/dashboards" \
  --data-binary @dashboards/traffic-and-performance.json
```

Then set the **Service**, **AEM environment** and **AEM tier** variables at the
top. They are driven by label values, so an environment that did not set
`aem_env_id` offers nothing for that variable - which is why it is worth setting
even with a single environment.

## Check it worked

Do these in order. Each one narrows the problem down.

**1. Wait for AEM to forward, then watch the function run.** Forwarding is on a
schedule, not instant, so allow up to an hour for the first object. To avoid
waiting, copy an AEM log file into the bucket yourself.

```bash
aws logs tail /aws/lambda/grafana-cloud-adobe-aem --since 1h --follow
```

A successful invocation logs the line count for each object:

```json
{"level":"INFO","message":"object shipped","uri":"s3://acme-aem-logs/aem/author_aemaccess_2026-07-24.log","lines":22747}
```

**2. Confirm the log types and labels.** In *Explore*, pick your Loki data source
and run:

```logql
sum by (log_type, aem_tier) (count_over_time({service_name="adobe-aem"}[1h]))
```

You should get one row per log type you forwarded. The Terraform `loki_query`
output and the CloudFormation `LokiQuery` output give you the bare selector with
your `service_name` already filled in.

**3. Confirm the fields were parsed.** This is the thing that makes the example
worth deploying, so check it explicitly:

```logql
sum by (status_class) (count_over_time({service_name="adobe-aem", log_type="aemcdn"}[1h]))
```

Rows like `2xx`, `4xx`, `5xx` mean parsing is working. Nothing at all usually
means either that log type is not being forwarded, or the object key did not
identify it. Check for `log_type="unknown"` in the query from step 2.

**4. Check the shipper's own health panels.** Open *Adobe AEM - Errors and
Operations*. Three panels watch this function rather than AEM:

- **Unparsed lines** should be 0. Non-zero means a log line arrived in a shape no
  parser recognised. It was still shipped, as raw text, so nothing is lost.
- **Uncorrelated responses** should be small and non-zero. That is the normal
  file-boundary cost of pairing request-log lines within one object.
- **Log lines/sec by type** shows a type dropping to zero, which means forwarding
  stopped.

**5. Check nothing is stuck.** The dead-letter queue should be empty:

```bash
# Terraform
DLQ_URL=$(terraform -chdir=terraform output -raw dlq_url)

# CloudFormation
DLQ_URL=$(aws cloudformation describe-stacks --stack-name grafana-cloud-adobe-aem \
  --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueUrl'].OutputValue" --output text)

aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages
```

## Configuration

Every setting is an environment variable on the function, with a matching
Terraform variable and CloudFormation parameter. Prefer changing it there.

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
| `MAX_PENDING_REQUESTS` | `20000` | Correlation buffer cap |
| `DROP_CLIENT_IP` | `false` | Omit `client_ip` from metadata |
| `LOG_UTC_OFFSET_SECONDS` | `0` | For the two log formats that carry no timezone |
| `MAX_TIMESTAMP_AGE_SECONDS` | `0` (off) | Stamp older lines at ingestion time. Set before a replay |
| `SOURCE_KEY_SUFFIX` | empty | Only process keys with this suffix |
| `LOKI_STATIC_LABELS` | `{}` | JSON object of extra labels. Low cardinality only |
| `LOKI_BATCH_MAX_LINES` | `5000` | Lines per Loki push |
| `LOKI_BATCH_MAX_BYTES` | `4194304` | Approximate bytes per Loki push |
| `LOG_LEVEL` | `INFO` | The function's own logging, not the data being shipped |

Leave `SOURCE_KEY_SUFFIX` empty unless the bucket holds more than AEM logs:
Adobe gzips some log types and not others, so any single suffix silently excludes
the rest.

### Labels, and what deliberately is not one

Labels are **deployment coordinates only**. Loki's stream count is the product of
every label's cardinality, so this list is short and every entry has a small,
bounded value set.

| Label | Where it comes from | Distinct values |
| --- | --- | --- |
| `service_name` | `SERVICE_NAME` | 1 |
| `aem_program_id` | `AEM_PROGRAM_ID`, or the object key | 1 per program |
| `aem_env_id` | `AEM_ENV_ID`, or the object key | 1 per environment |
| `aem_env_type` | `AEM_ENV_TYPE`, or the object key | dev / stage / prod |
| `aem_tier` | the object key, else `AEM_TIER` | author / publish / preview / dispatcher |
| `log_type` | the object key, else content sniffing | the 7 AEM log types |
| `level` | parsed, **only** on `aemerror`, `aemdispatcher`, `aemhttpderror` | 5 |

Ten environments across four tiers and seven log types is a few hundred streams.
An unset coordinate **omits its label** rather than setting a placeholder: a label
reading `unknown` still creates a stream and still looks like data.

Everything the parsers extract goes into **structured metadata** instead:
`path`, `status`, `status_class`, `method`, `client_ip`, `country`, `region`,
`pop`, `cache_status`, `duration_ms`, `ttfb_ms`, `ttlb_ms`, `bytes_sent`,
`user_agent`, `referer`, `remote_user`, `node_id`, `logger`, `thread`,
`exception`, `module`, `pid`, `tid`, `farm`, `host`, `request_id`, `direction`,
`correlated`, `content_type`, `object_key`, `record`, `parse_status`,
`detected_level`.

`status` is the one that looks like it should be a label and must not be. It is a
small set alone, but it multiplies every other label, and nobody queries it
without also slicing by path or method, which are unbounded.

### Querying structured metadata

**A metadata field goes after a `|`, never inside the `{}` selector.**

```logql
{log_type="aemcdn", cache_status="HIT"}     # WRONG: empty result, no error
{log_type="aemcdn"} | cache_status="HIT"    # right
```

The wrong form does not fail. It returns nothing, silently, because `{}` matches
labels only. That is the most likely reason a query against this data looks
broken.

Two more worth knowing:

- **`unwrap` needs an aggregation.** A bare
  `quantile_over_time(0.99, {...} | unwrap duration_ms [5m])` returns one series
  per log line. Add `by (...)`, or wrap it in `max()`.
- **Numeric comparison works**: `| duration_ms > 1000` is a valid filter.

Some queries to start from:

```logql
# p99 server-side latency by path
topk(15, quantile_over_time(0.99, {service_name="adobe-aem", log_type="aemrequest"}
  | unwrap duration_ms [$__range]) by (path))

# CDN cache hit ratio
sum(count_over_time({service_name="adobe-aem", log_type="aemcdn"} | cache_status="HIT" [$__range]))
  / sum(count_over_time({service_name="adobe-aem", log_type="aemcdn"}[$__range]))

# Which Java class is complaining
topk(15, sum by (logger) (count_over_time(
  {service_name="adobe-aem", log_type="aemerror", level=~"warn|error"}[$__range])))

# What this is costing, by log type
sum by (log_type) (bytes_over_time({service_name="adobe-aem"}[$__interval]))
```

### `level` versus `detected_level`

`level` is a label and is only ever what AEM wrote. The four HTTP log types get
`detected_level` in structured metadata instead, inferred from the status class
(5xx → error, 4xx → warn, else info). Keeping them apart means you never have to
guess which you are looking at.

## Object keys

Adobe does not document a single S3 key layout for forwarded logs. The
log-forwarding documentation specifies the *Azure Blob Storage* CDN filename
format and lists S3 CDN forwarding as a future capability, while the AEM and
Dispatcher logs land under a prefix you choose in Cloud Manager.

So the layout is a configurable regex, `KEY_PATTERN`, with named groups drawn
from `program_id`, `env_id`, `env_type`, `tier`, `log_type`. The default matches
Adobe's own download naming - `author_aemaccess_2026-07-24.log` - and is
*searched* rather than anchored, so any prefix in front of it is ignored.

For a bucket laid out by program and environment:

```hcl
key_pattern = "p(?P<program_id>[0-9]+)/e(?P<env_id>[0-9]+)/(?P<tier>author|publish|preview)/(?P<log_type>aem[a-z]+)/"
```

Two safety behaviours worth knowing. A pattern that captures none of the
recognised groups is **rejected at cold start**, because one that matches every
object while extracting nothing is indistinguishable from one that works. And if
the pattern finds no log type, the first line's shape is used instead, so a
mismatched pattern degrades rather than breaking - and says so in the function
logs.

## The seven log types

| `log_type` | Tier | Verified against | Notes |
| --- | --- | --- | --- |
| `aemaccess` | author, publish, preview | real output | Apache-like, node id first |
| `aemerror` | author, publish, preview | real output | Extracts the Java exception, including from stack-trace lines |
| `aemrequest` | author, publish, preview | real output | Paired `->`/`<-` lines |
| `aemcdn` | author, publish, preview | real output | JSON Lines; fields renamed so one panel spans CDN and access |
| `aemdispatcher` | publish | **Adobe docs only** | Trailing fields vary by dispatcher build |
| `aemhttpdaccess` | publish | **Adobe docs only** | Same format as `aemaccess`; only the key distinguishes them |
| `aemhttpderror` | publish | **Adobe docs only** | Apache error log |

The three marked *Adobe docs only* are built from the published format strings
and tested against hand-written fixtures, because no real sample was available.
If one does not parse your output it shows up as `parse_status="unmatched"` on
the operations dashboard - please report it.

**No line is ever dropped.** A line no parser recognises ships as raw text with
`parse_status="unmatched"`. A shipper that silently discards what it cannot parse
is worse than one that does no parsing, because the gap is invisible.

Two formats carry no timezone - the AEM Java error log and the Apache error log.
AEM as a Cloud Service writes UTC, so the default is correct; set
`LOG_UTC_OFFSET_SECONDS` for a self-hosted AEM that logs local time.

## What it costs

- **Loki ingest** dominates, and it is per GB.

  The thing that actually drives your bill is AEM's own volume. On a sampled
  author-tier day, `/systemready` and `login.html` health checks were **76% of
  the access log and 98% of the CDN log**. Filter those out in Cloud Manager's
  forwarding configuration, not here: a line this function never receives is
  free, and a line it drops has already been paid for. The *Ingested bytes by log
  type* panel exists to make this obvious.

  `LINE_CONTENT=message` drops the timestamp and node id from the log line, since
  both are already in metadata - roughly 40% off an error log, at the cost of
  `|=` no longer matching against the original text.

- **Structured metadata** is not free, but it replaces a parse stage on every
  query from every viewer, and keeps stream count flat as traffic grows.
- **Lambda** on GB-seconds. Parsing is CPU-bound, so more memory means
  proportionally more CPU and a shorter run; 1024 MB is usually the sweet spot.
- **CloudWatch Logs** for the function's own output. Leave `LOG_LEVEL` at `INFO`.
- **S3 GET requests**, one per object, plus SQS requests per notification.

## Troubleshooting

**Everything is in `log_type="unknown"`.** The `KEY_PATTERN` does not match your
bucket, and content sniffing could not identify the format either. Look at a real
key and set the pattern. The function logs
`log type identified by content, not key` when it falls back.

**`aemaccess` and `aemhttpdaccess` are mixed up.** They are byte-for-byte the
same format, so content alone cannot tell them apart. Only the object key can -
make sure your `KEY_PATTERN` captures `log_type`.

**Nothing arrives, and the function never runs.** Either AEM has not forwarded
yet (it is on a schedule, allow an hour), or the bucket notification is not
wired:

```bash
aws s3api get-bucket-notification-configuration --bucket acme-aem-logs
```

**`401` in the function logs.** Three causes, and the body says which:

| Body | Cause |
| --- | --- |
| `invalid authentication credentials` | Wrong tenant id for that token, or a Grafana service-account token instead of a Cloud Access Policy token |
| `invalid scope requested` | Live token, but its policy lacks `logs:write` |
| `invalid token` | Revoked or malformed |

**`400` with `entry too far behind`.** AEM's own timestamps are older than your
tenant's `reject_old_samples_max_age`, which is a week on Grafana Cloud by
default. Normal forwarding is near-real-time so this usually only bites on a
replay of historical logs. Set `MAX_TIMESTAMP_AGE_SECONDS` so old lines are
stamped at ingestion instead of failing the batch they travelled in.

**`429` in the logs.** Your tenant's rate limit, usually on a first run against a
bucket with a backlog. Set `reserved_concurrency` to a real number, starting
with 10.

**Latency panels are empty.** They read `duration_ms`, which only exists on the
request log's response lines. Check `aemrequest` is being forwarded and that
`CORRELATE_REQUESTS` is on.

**Dashboard variables offer no choices.** They are driven by label values, so a
label this deployment never set has nothing to list. Set `AEM_ENV_ID` and
re-deploy; data already in Loki keeps the labels it was written with.

**The function times out.** One object is a whole hour or day of logs. Raise
`timeout_seconds` towards the 900 s Lambda maximum, and `memory_mb` with it,
since memory buys CPU.

**Objects processed twice.** Expected after a retry, and harmless: Loki
deduplicates entries identical in timestamp, labels and line content within a
stream.

**Something is in the dead-letter queue.** The message carries the original S3
notification, so redriving it after a fix reprocesses exactly what failed:

```bash
# Terraform
DLQ_ARN=$(terraform -chdir=terraform output -raw dlq_arn)

# CloudFormation
DLQ_ARN=$(aws cloudformation describe-stacks --stack-name grafana-cloud-adobe-aem \
  --query "Stacks[0].Outputs[?OutputKey=='DeadLetterQueueArn'].OutputValue" --output text)

# The ARN, not the URL - start-message-move-task will not take a URL.
aws sqs start-message-move-task --source-arn "$DLQ_ARN"
```

## Limitations

- **Request/response pairing is within one object only.** A request whose
  response lands in the next hour's file is never paired, and ships with
  `correlated="false"`. Measured at 100% of responses paired within a real
  author-tier day's file; the residue is the file boundary.
- **Three parsers are unverified against production output** - see the table
  above.
- **The S3 key layout is a guess until you configure it.** Adobe publishes no
  single layout.
- One Loki tenant per deployed function. Two tenants means two deployments.
- Objects are never deleted or moved after processing. Use an S3 lifecycle rule
  if you need process-then-archive; doing it in the function makes the retry
  semantics much harder to reason about.
- The dashboards are Grafana schema v2. That is the current default and a v1
  dashboard is converted on import, but a much older Grafana may not accept them.

## How it works

S3 notification → SQS → Lambda → parse → Loki push API.

**Why the fields are parsed on the way in.** A raw shipper puts the whole line in
Loki and leaves every query to re-parse it, so every panel carries a `| json` or
`| regexp` stage and every viewer pays for that parse on every dashboard load.
Parsing once at write time moves that cost to one place and makes the fields
aggregatable directly.

**Why the request log is correlated.** AEM splits one HTTP request across two
lines: the first carries the method and path, the second carries the status and
the duration. Neither is useful alone, and Loki has no join, so no query can put
them back together. Pairing them on ingest is what makes latency-by-path possible
at all.

**Why SQS rather than invoking from S3 directly.** A direct notification gives
one invocation per object, no batching, two retries and then silence. The queue
gives batching, a visibility timeout you control, a retry policy, a dead-letter
queue you can inspect, and partial batch failure so one unreadable object does
not force redelivery of the whole batch.

**Why the Loki push API rather than OTLP.** OTLP's resource-attribute promotion
model indexes the wrong things here: only specific resource attributes become
labels without per-stack configuration, and an AEM log's useful dimensions are
not resource attributes.

## More detail

These live in the public repository rather than in this download:

- [Loki label and batching rules](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/loki-ingestion.md)
- [Getting Grafana Cloud credentials](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/grafana-cloud-credentials.md)
- [OTLP versus the Loki push API](https://github.com/rknightion/grafana-cloud-reference-examples/blob/main/docs/otlp-vs-loki-push.md)
- [generic-s3, the format-agnostic version of this example](https://github.com/rknightion/grafana-cloud-reference-examples/tree/main/examples/generic-s3)
- [Adobe: log forwarding](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/log-forwarding)
- [Adobe: logging](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/logging)

Working on this example from a clone:

```bash
just test                    # from the repo root
just package adobe-aem       # builds dist/adobe-aem-<version>.zip
just aem-dashboards          # regenerate dashboards/ from dev/build_dashboards.py
just aem-push fixtures       # push the test corpus to a Loki tenant, no AWS needed
```

Test fixtures live in `fixtures/` in the repository, with their provenance and
sanitisation recorded in `fixtures/README.md`. They are real AEM output with
every identifier rewritten.
