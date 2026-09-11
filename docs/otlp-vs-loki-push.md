# OTLP logs or a direct Loki push?

**Direct Loki push, for the kind of work this repo does.** OTLP logs is a real
option and a good one for an instrumented application shipping its own logs. It
is the wrong shape for arbitrary third-party records read out of a file, and this
page says why so the question does not get re-opened per example.

## The deciding constraint: what gets indexed

Grafana Cloud's OTLP gateway promotes a fixed set of **resource attributes** to
Loki labels. Everything else is kept as **structured metadata** - not dropped,
still queryable, but not part of the stream selector.

That promotion list is built around service and infrastructure topology:
`service.name`, `service.namespace`, `service.instance.id`,
`deployment.environment.name`, the `cloud.*` attributes and the `k8s.*`
attributes. Confirm the current list in Grafana's own docs rather than trusting
any copy of it, including this one:
<https://grafana.com/docs/loki/latest/get-started/labels/>

That list is exactly right when the producer is an instrumented service, because
those attributes are things it genuinely has. It is exactly wrong for a Lambda
shipping someone else's export out of S3: the meaningful dimensions are the
bucket, the dataset, the sandbox, the export type - none of which are promoted.
You would either invent fake resource attributes to get anything indexed, or
accept that every dimension you actually select on lives in structured metadata.
That is still queryable, but only as post-selector filtering: the stream selector
matches far more data than you want, and the filter runs over it afterwards. That
is the query-performance problem Loki's label model exists to avoid.

A direct push lets the example choose its stream labels explicitly, per batch.

## The other two reasons

**The OTel Python logs SDK is still experimental.** It lives at
`opentelemetry.sdk._logs` - the underscore is the maintainers' own signal - and
its docs state that APIs may change in minor or patch releases with no backward
compatibility guarantee. Fine for an application you control end to end; poor for
a reference implementation whose whole job is to keep working unattended.

**Logs have no auto-flush in Lambda.** `AwsLambdaInstrumentor` accepts a
`tracer_provider` and a `meter_provider` and flushes those on invocation exit. It
takes no `logger_provider`. `BatchLogRecordProcessor` exports on a background
thread, and that thread is exactly what the execution environment freezes when
the handler returns - so you must call `logger_provider.force_flush()` yourself,
or switch to the synchronous processor. Either is workable; both are hand-rolled
flush safety around an experimental API, for no label-fidelity gain.

## Promotion is configurable, and that does not change the conclusion

Grafana Cloud has a self-serve surface for changing promotion, per stack: a UI
under **Databases Configuration → Logs → OTLP config**, and an API at
`PUT <LOKI_URL>/loki/api/v1/config/limits/otlp_config`, with three actions per
attribute (`index_label`, `structured_metadata`, `drop`). The PUT **replaces**
the whole config rather than merging, so read `GET .../config/limits/applied`
first.

There is a documented cap on promoted attributes, and Grafana's docs currently
give two different numbers in two places (30 in the self-serve config docs, 15 in
the ingestion limits table). Read your own stack's applied config before relying
on either.

The reason this does not change the answer: making OTLP work for arbitrary
records requires every customer to configure their stack before the example
ingests anything usefully. An example whose prerequisite is a tenant-level
configuration change is a worse example, even when the change is self-serve.

## When OTLP logs is the right answer

- The producer is an **OTel-instrumented service** emitting its own logs, and you
  want `trace_id`/`span_id` correlation with its traces.
- Its resource attributes already match the promotion list, so the labels you
  want indexed are the labels you get.
- You are shipping logs, metrics and traces over one pipeline and one credential.

That is a legitimate future example - "correlate an instrumented service's logs
with its traces in Grafana Cloud" - and it should be written as its own example
with its own README. It is not a second sink to bolt onto a file shipper.

## What is documented per example

Each example's README names its stream labels and says which fields go to
structured metadata instead. An example that *does* use OTLP must additionally
state which resource attributes it sets, which of those the default promotion
list indexes, and what a customer has to change if they want more - because
without that, a deployer cannot tell why their new attribute is not selectable.
