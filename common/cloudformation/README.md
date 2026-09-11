# CloudFormation

| File | Role |
| --- | --- |
| [`templates/lambda-s3-loki.reference.yaml`](templates/lambda-s3-loki.reference.yaml) | The canonical base. Copy it to start a new example |
| [`conformance.yaml`](conformance.yaml) | What every example's template must carry. Enforced by `just lint` |

## Reuse here is by copy plus enforcement, not by nested stacks

Each example ships one standalone, flat `template.yaml`. That is deliberate.

A nested stack (`AWS::CloudFormation::Stack`) needs its child templates staged in
an S3 bucket before the root stack can be created. That breaks the single
property that makes these examples useful: one downloaded zip, deployable on its
own, with nothing to upload first.

So the templates duplicate each other, and `tools/check_conformance.py` keeps the
duplication honest. Examples may diverge freely on what they actually do. They
may not diverge on any of the rules in `conformance.yaml`: a finite log
retention, an explicit log group, a dead-letter queue, `ReportBatchItemFailures`,
a closed runtime allowlist with a GA default, a preview-runtime gate, no
wildcard IAM, no credential-shaped parameter with a default, and no
credential-shaped environment variable.

Add a rule there when you find a mistake worth never making twice. Do not add one
for style.

## Two things CloudFormation cannot do, and what this does instead

**It cannot upload function code from your machine.** `AWS::Lambda::Function`
takes an S3 bucket and key, so the zip must be in S3 first. The Terraform path
uploads it for you; that is the only real difference between the two paths.

**It cannot add a notification configuration to a bucket it does not own.**
`AWS::S3::Bucket`'s `NotificationConfiguration` only applies to a bucket the stack
creates, and these examples read from a bucket that already exists. The reference
template therefore offers two wirings:

- `EventWiring: EventBridge` creates an `AWS::Events::Rule` routing the bucket's
  `Object Created` events to the queue. Fully declarative, and safe on a shared
  bucket. Needs the bucket's EventBridge notifications enabled once.
- `EventWiring: Manual` creates nothing and outputs the exact
  `aws s3api put-bucket-notification-configuration` command. Note that this call
  replaces the bucket's **entire** notification configuration, so merge rather
  than overwrite if the bucket already has one.

A Lambda-backed custom resource would automate the second case. It is not used:
it adds a second function and role to every example, and it fails in ways that
are much harder for a reader to diagnose than one documented CLI call.

## An EventBridge filter cannot AND a prefix with a suffix

Multiple matchers on one field in an EventBridge pattern are **OR**'d. So
`"key": [{"prefix": "logs/"}, {"suffix": ".gz"}]` widens the filter to
"prefix OR suffix" rather than narrowing it to both - the opposite of the intent.

The reference template therefore filters on prefix only and passes the suffix to
the function as `SOURCE_KEY_SUFFIX`, which skips a non-matching key. The S3
notification path does support both as real filter rules, so it applies them
there.

## There is no assertion primitive

CloudFormation has no way to say "fail if this condition holds". The
preview-runtime guard is an `AWS::CloudFormation::WaitCondition` that exists only
in the bad case and cannot succeed: it creates no infrastructure, costs nothing,
and fails the stack with the reason in the events. Reuse that pattern rather than
inventing a new one.
