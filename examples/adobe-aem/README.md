# adobe-aem: Adobe Experience Manager Cloud Service logs to Grafana Cloud Loki

An AWS Lambda that picks up the logs Adobe Experience Manager as a Cloud Service
forwards into S3, parses all seven log types, and ships them to Grafana Cloud
Loki with the fields already extracted. Ships two dashboards.

**Status: alpha.** Four of the seven parsers are tested against real AEM output;
three are built from Adobe's documented formats. See
[What is verified, and what is not](#what-is-verified-and-what-is-not).

## Why this rather than a raw log shipper

A raw shipper puts the whole log line in Loki and leaves every query to re-parse
it. Every panel then carries a `| json` or `| regexp` stage, and every viewer
pays for that parse on every dashboard load.

This parses each line **once, at write time**, and puts the result in Loki
**structured metadata**. A dashboard then filters and aggregates on
`status_class`, `cache_status`, `path`, `duration_ms`, `logger` or `exception`
directly - no parser stage - while the stream labels stay down to seven bounded
deployment coordinates.

The clearest example is latency. The AEM request log splits one HTTP request
across two lines: the first has the method and path, the second has the status
and the duration. Neither is useful alone, and Loki has no join, so no query can
put them back together. This pairs them on ingest, which is what makes

```logql
topk(15, quantile_over_time(0.99, {log_type="aemrequest"} | unwrap duration_ms [$__range]) by (path))
```

possible at all. On a real author-tier day - 45,824 lines, 22,912 responses -
that pairs 100% of responses.

## What it costs

Structured metadata is not free, so the honest summary is: this trades a modest
increase in bytes per line for the removal of a parse stage from every query,
and for a stream count that stays flat as traffic grows.

The thing that actually drives the bill is AEM's own volume. On the sampled
author-tier day, `/systemready` and `login.html` health checks were 76% of the
access log and 98% of the CDN log. **Filter those out in Cloud Manager's
forwarding configuration, not here** - a line this function never receives is
free, and a line it drops has already been paid for. The
`Ingested bytes by log type` panel on the operations dashboard is there to make
that obvious.

`LINE_CONTENT=message` drops the timestamp and node id from the log line, since
both are already in metadata. That is roughly 40% off an error log, at the cost
of `|=` no longer matching against the original text.

## The label contract

**Labels are deployment coordinates only.** Stream count is the product of every
label's cardinality, so this list is deliberately short and every entry has a
small, bounded value set.

| Label | Source | Values |
|---|---|---|
| `service_name` | `SERVICE_NAME` | 1 |
| `aem_program_id` | `AEM_PROGRAM_ID`, or the object key | 1 per program |
| `aem_env_id` | `AEM_ENV_ID`, or the object key | 1 per environment |
| `aem_env_type` | `AEM_ENV_TYPE`, or the object key | dev / stage / prod |
| `aem_tier` | the object key, else `AEM_TIER` | author / publish / preview / dispatcher |
| `log_type` | the object key, else content sniffing | the 7 AEM log types |
| `level` | parsed, **only** on `aemerror`, `aemdispatcher`, `aemhttpderror` | 5 |

Ten environments across four tiers and seven log types is a few hundred streams.
A test asserts the whole 1,036-line fixture corpus fits in 25.

An unset coordinate **omits its label** rather than setting a placeholder. A
label reading `unknown` still creates a stream and still looks like data.

Everything else is structured metadata: `path`, `status`, `status_class`,
`method`, `client_ip`, `country`, `region`, `pop`, `cache_status`, `duration_ms`,
`ttfb_ms`, `ttlb_ms`, `bytes_sent`, `user_agent`, `referer`, `remote_user`,
`node_id`, `logger`, `thread`, `exception`, `module`, `pid`, `tid`, `farm`,
`host`, `request_id`, `direction`, `correlated`, `content_type`, `object_key`,
`record`, `parse_status`, `detected_level`.

`status` is the one that looks like it should be a label. It must not be: it is a
small set alone, but it multiplies every other label, and nobody queries it
without also slicing by path or method, which are unbounded.

### The LogQL rule this forces

**A structured metadata field goes after a `|`, never inside the `{}` stream
selector.**

```logql
{log_type="aemcdn", cache_status="HIT"}     # WRONG: empty result, no error
{log_type="aemcdn"} | cache_status="HIT"    # right
```

The wrong form does not fail. It returns nothing, silently, because `{}` matches
labels only. That is the single most likely reason a query against this data
comes back empty.

Two more, both verified live:

- **`unwrap` needs an aggregation.** A bare
  `quantile_over_time(0.99, {...} | unwrap duration_ms [5m])` returns one series
  per log entry. Add `by (...)`, or wrap it in `max()`.
- **Numeric comparison works on metadata**: `| duration_ms > 1000` is a valid
  filter.

## What this treats as sensitive, and what it does not

**IP addresses are shipped by default, and that is deliberate.** A client
address in an operational log is ordinary, useful telemetry - abuse
investigation, geography, separating health-check traffic from real users all
depend on it - and Grafana Cloud Loki is full of them by design. `DROP_CLIENT_IP`
exists for a deployment with a data-protection position that forbids storing
them; it is not the default because for most people it would throw away signal
for no gain.

The data that actually needs care is content-level, and no log shipper can
reliably find it:

- medical details and health records
- home and postal addresses
- dates of birth
- phone numbers
- national identifiers and payment details

None of that belongs in a log line in the first place. AEM will happily log it if
an application puts it in a request path, a query string or an exception message,
and at that point the fix is in the application or in Cloud Manager's forwarding
configuration - not here. This example has three levers if you need them:
`LINE_CONTENT=message` drops the raw line, `DROP_CLIENT_IP` drops addresses, and
`SOURCE_KEY_SUFFIX` plus Cloud Manager filtering stop whole log types arriving.

The fixture tooling is stricter than the shipper on purpose, because a public
repository is not a Loki tenant. `tools/sanitise_aem_logs.py` **refuses to write
a corpus** containing any of the categories above, and the repository's own
`just sanitise-aem-logs` recipe passes `--ip-addresses replace` even though the
tool's default is to keep them. See `fixtures/README.md`.

### `level` versus `detected_level`

`level` is a label and is only ever what AEM wrote. The four HTTP log types get
`detected_level` in metadata instead, inferred from the status class
(5xx → error, 4xx → warn, else info).

Keeping them separate means nobody has to guess which they are looking at. It is
also worth setting: Grafana Cloud fills `detected_level` in itself when the field
is absent, and for these formats it cannot tell - every such entry arrived
carrying `detected_level="unknown"` before this was added.

## The seven log types

| `log_type` | Tier | Format | Notes |
|---|---|---|---|
| `aemaccess` | author, publish, preview | Apache-like, with a node id first | |
| `aemerror` | author, publish, preview | `date [node] *LEVEL* [thread] logger message` | Extracts the Java exception, including from stack-trace lines |
| `aemrequest` | author, publish, preview | paired `->` / `<-` lines | Paired on ingest; see above |
| `aemdispatcher` | publish | `[date] [L] [node] "req" status duration [farm] [cache] "host"` | Trailing fields vary by build |
| `aemhttpdaccess` | publish | same as `aemaccess` | Only the object key distinguishes the two |
| `aemhttpderror` | publish | `Day Mon DD HH:MM:SS.ffffff YYYY [module:level] [pid]` | |
| `aemcdn` | author, publish, preview | JSON Lines | Fields renamed to the shared vocabulary so one panel spans CDN and access |

**No line is ever dropped.** A line no parser recognises is shipped as raw text
with `parse_status="unmatched"`, and the operations dashboard counts those. A
shipper that silently discards what it cannot parse is worse than one that does
no parsing, because the gap is invisible.

Two timestamp formats carry no timezone - the AEM error log and the Apache error
log. AEM as a Cloud Service writes UTC; set `LOG_UTC_OFFSET_SECONDS` for a
self-hosted AEM that does not.

## Object keys

Adobe does not document a single S3 key layout for forwarded logs. The
log-forwarding documentation specifies the *Azure Blob Storage* CDN filename
format and lists S3 CDN forwarding as a future capability, while the AEM and
Dispatcher logs land under a prefix chosen in Cloud Manager.

So the layout is a configurable regex, `KEY_PATTERN`, with named groups from
`program_id`, `env_id`, `env_type`, `tier`, `log_type`. The default matches
Adobe's own download naming (`author_aemaccess_2026-07-24.log`) and is *searched*
rather than anchored, so any prefix in front of it is ignored.

Check your real keys before deploying:

```bash
aws s3 ls s3://your-aem-log-bucket/ --recursive | head
```

A pattern that captures nothing is rejected at cold start, because a pattern
matching every object while extracting nothing is indistinguishable from one
that works - and the consequence is a month of logs in the `unknown` stream.

If the pattern finds no log type, the first line's shape is used instead. That
keeps a mismatched pattern degrading rather than breaking, and it is logged so
you can fix the pattern.

## Deploy

Both paths deploy the same thing. Terraform uploads the function code for you;
CloudFormation cannot, so the zip has to be in S3 first.

Create the secret before either. The token is never an input to either tool, so
it never lands in state:

```bash
umask 077 && cat > /tmp/gc-loki.json <<'JSON'
{"tenant_id":"123456","token":"glc_..."}
JSON
aws secretsmanager create-secret --name grafana-cloud/loki \
  --secret-string file:///tmp/gc-loki.json
rm -f /tmp/gc-loki.json
```

### Terraform

```bash
cp terraform/terraform.tfvars.example terraform/terraform.tfvars
$EDITOR terraform/terraform.tfvars
cd terraform && terraform init && terraform apply
```

### CloudFormation

```bash
aws s3 cp lambda.zip s3://my-deployment-artifacts/adobe-aem/lambda.zip
aws cloudformation deploy \
  --template-file cloudformation/template.yaml \
  --stack-name grafana-cloud-adobe-aem \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides file://cloudformation/parameters.example.json
```

## Dashboards

Two, in `dashboards/`, as Grafana dashboard schema **v2**
(`dashboard.grafana.app/v2`), which is the current preferred version - a v1
dashboard is converted to v2 on import.

- **`traffic-and-performance.json`** - request rate, status classes, CDN cache
  hit ratio, CDN and AEM latency percentiles, slowest and busiest paths,
  geography, and a failed-request log panel.
- **`errors-and-operations.json`** - error and warning rates by level, top
  loggers, Java exceptions, per-node volume, dispatcher cache outcome, ingested
  bytes by log type, and three panels watching **this shipper** rather than AEM:
  unparsed lines, uncorrelated responses, and per-type volume.

Import through the UI, or:

```bash
curl -X POST -H "Authorization: Bearer $GRAFANA_SA_TOKEN" \
  -H 'Content-Type: application/json' \
  "https://<your-stack>.grafana.net/apis/dashboard.grafana.app/v2/namespaces/stacks-<stack-id>/dashboards" \
  --data-binary @dashboards/traffic-and-performance.json
```

Set the `service`, `env_id` and `tier` variables after import. They are driven by
`label_values`, so a label this deployment leaves empty offers no choice - which
is why `aem_env_id` is worth setting even with one environment.

The dashboards are generated from `dev/build_dashboards.py`, because 60 lines of
nested v2 `kind`/`spec` envelopes per panel is not reviewable in a diff. Edit the
generator and run `just aem-dashboards`; a test fails if the committed JSON drifts.

## Trying it without AWS

`dev/run_local.py` runs the real parsers, the real labelling and the real Loki
client against local data. Useful for confirming a dashboard query before
deploying anything.

```bash
export GRAFANA_CLOUD_LOKI_ENDPOINT=https://logs-prod-012.grafana.net
export GRAFANA_CLOUD_LOKI_TENANT_ID=123456
export GRAFANA_CLOUD_LOKI_TOKEN=glc_...
export AEM_PROGRAM_ID=p12345 AEM_ENV_ID=e67890 AEM_ENV_TYPE=dev

just aem-push fixtures                    # the committed corpus, once
just aem-push synth --rounds 0            # continuous fake traffic
just aem-push fixtures --dry-run          # summarise streams, push nothing
```

Both modes shift timestamps into the recent past, **per file**. They have to:
Loki rejects a sample older than the tenant's `reject_old_samples_max_age` (one
week by default) with a 400 that no retry fixes and that fails the whole batch.

`synth` is not the fixtures on a loop. It generates a long tail of paths, a
realistic cache hit distribution, a trickle of crawler 404s and lognormal
latencies, because a dashboard built against eleven repeating paths and two
status codes looks finished and then falls over on real data.

## What is verified, and what is not

**Verified against real AEM Cloud Service output**, from a sanitised author-tier
day: `aemaccess`, `aemerror`, `aemrequest`, `aemcdn`. 1,000 lines, zero
unparsed, every timestamp extracted.

**Built from Adobe's documented format strings, not from real output**:
`aemdispatcher`, `aemhttpdaccess`, `aemhttpderror`. They are tested against
hand-written fixtures matching the documentation. Treat them as unverified
against production, and please report a line they do not parse - it will show up
as `parse_status="unmatched"` on the operations dashboard.

**Verified against a live Grafana Cloud Loki tenant**: every panel query in both
dashboards, and the structured-metadata aggregation the whole design depends on.

**Not verified**: the real S3 key layout Adobe writes (hence `KEY_PATTERN`), and
an end-to-end deploy against a live AEM forwarding configuration.

## Test fixtures

`fixtures/` holds 1,036 lines of sanitised log data, used by the tests and by
`dev/run_local.py`. `fixtures/README.md` records their provenance and how they
were sanitised.

They are real AEM output with every identifier rewritten: program and environment
ids to `p12345`/`e67890`, the AEM tenant to `examplecorp`, pod names to
deterministic fakes, authenticated user emails to `user<n>@example.com`, and
every client address into RFC 5737 documentation ranges. Regenerate with
`just sanitise-aem-logs <directory>`, then **run `just public-release-scan`** -
the sanitiser is best-effort and the scan is the gate.

## Configuration

Every setting is an environment variable on the function, with a matching
Terraform variable and CloudFormation parameter.

| Variable | Default | Purpose |
|---|---|---|
| `GRAFANA_CLOUD_LOKI_ENDPOINT` | required | Host, base URL or full push URL |
| `GRAFANA_CLOUD_LOKI_TENANT_ID` | required | Numeric Loki tenant id |
| `GRAFANA_CLOUD_CREDENTIALS_SECRET_ID` | required | Secrets Manager secret holding the token |
| `SERVICE_NAME` | `adobe-aem` | `service_name` label |
| `KEY_PATTERN` | see above | Regex with named coordinate groups |
| `AEM_PROGRAM_ID` / `AEM_ENV_ID` / `AEM_ENV_TYPE` | empty | Labels the key does not carry |
| `AEM_TIER` | empty | Fallback tier; a tier in the key wins |
| `LINE_CONTENT` | `raw` | `raw` or `message` |
| `SNIFF_CONTENT` | `true` | Identify the log type from content when the key does not |
| `CORRELATE_REQUESTS` | `true` | Pair request-log line pairs |
| `MAX_PENDING_REQUESTS` | `20000` | Correlation buffer cap |
| `DROP_CLIENT_IP` | `false` | Omit `client_ip` from metadata |
| `LOG_UTC_OFFSET_SECONDS` | `0` | For the two timezone-free formats |
| `MAX_TIMESTAMP_AGE_SECONDS` | `0` | Stamp older lines at ingestion time; set before a replay |
| `SOURCE_KEY_SUFFIX` | empty | Key suffix filter |
| `LOKI_STATIC_LABELS` | `{}` | JSON object of extra stream labels. Low cardinality only |
| `LOKI_BATCH_MAX_LINES` | `5000` | Log lines per Loki push |
| `LOKI_BATCH_MAX_BYTES` | `4194304` | Approximate bytes per Loki push |
| `LOG_LEVEL` | `INFO` | The function's own logging |

## Related

- [`generic-s3`](../generic-s3) - the format-agnostic version, and the base this
  extends
- [`docs/loki-ingestion.md`](../../docs/loki-ingestion.md) - the repository-wide
  label rules
- [`docs/otlp-vs-loki-push.md`](../../docs/otlp-vs-loki-push.md) - why this
  pushes to Loki directly rather than via OTLP
- [Adobe: log forwarding](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/log-forwarding)
- [Adobe: logging](https://experienceleague.adobe.com/en/docs/experience-manager-cloud-service/content/implementing/developing/logging)
