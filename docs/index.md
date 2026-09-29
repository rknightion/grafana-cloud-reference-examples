---
title: grafana-cloud-reference-examples
description: Working examples of getting data into Grafana Cloud, and of wiring Grafana Cloud up to other products. Self-contained, downloadable Terraform and CloudFormation deployments.
---

# grafana-cloud-reference-examples

Working examples of getting data into Grafana Cloud, and of wiring Grafana Cloud up to other
products. Scripts, Lambda functions, containers, whatever the job needs.

Each example is a self-contained deliverable: download one zip, read one README, deploy with
either Terraform or CloudFormation. They are reference implementations meant to be read, forked
and adapted, not a supported product.

This is a personal reference project, not an official Grafana Labs product. Nothing here is
deployed by this repository itself; every example is something you deploy into your own account.

## Examples

| Example | Source | Destination | Deliverable | Status | What it does |
| --- | --- | --- | --- | --- | --- |
| [Adobe AEM to Loki](examples/adobe-aem.md) | S3 (AEM Cloud Service log forwarding) | Grafana Cloud Loki | Lambda zip | alpha | Parses all seven AEM log types and ships two dashboards |
| [Generic S3 to Loki](examples/generic-s3.md) | S3 (any bucket) | Grafana Cloud Loki | Lambda zip | alpha | Text, JSON Lines, JSON array or CSV objects, optionally gzipped |
| [DocumentDB connection attribution](examples/docdb-connection-attribution.md) | Amazon DocumentDB audit log + `$currentOp` | Grafana Cloud (OTLP) | Lambda zip | alpha | Attributes open connections to the database user that opened them, as a gauge, with a dashboard |

`just examples` in the repository prints this table from the `example.yaml` manifests, which are
the single source of truth; `just check` fails if the root README's own copy of this table drifts
from them.

**Which one do you want?** If your data is AEM Cloud Service log forwarding, take
[Adobe AEM to Loki](examples/adobe-aem.md) - it parses the formats and comes with dashboards. For
anything else landing in S3, start from [Generic S3 to Loki](examples/generic-s3.md) and add a
parser. For DocumentDB, see [DocumentDB connection attribution](examples/docdb-connection-attribution.md).

## Deploying a release bundle

Grab the latest zip for the example you want from
[Releases](https://github.com/rknightion/grafana-cloud-reference-examples/releases). Tags are
per-example, so `generic-s3-v1.2.0` only ever changes `generic-s3`.

The zip contains the function code, the shared library it needs, a Terraform root module and an
equivalent single-file CloudFormation template. Pick one; they deploy the same thing.

```
generic-s3-v1.2.0.zip
├── README.md               prerequisites, deploy, verify, troubleshoot
├── lambda.zip              ready to upload, or let the IaC upload it
├── terraform/              root module + vendored modules/, no backend block
├── cloudformation/         one flat template.yaml + parameters.example.json
├── dashboards/             importable Grafana dashboards, where the example has them
├── MANIFEST.json           version, runtime, and the sha256 of lambda.zip
└── LICENSE
```

Every example README follows the same shape, so once you have deployed one the next is familiar:
what you need before you start, what it creates in your AWS account, deploy it, check it worked,
configuration, what it costs, troubleshooting, limitations.

You supply a Grafana Cloud tenant id and a Cloud Access Policy token scoped to what the example
writes. See [Grafana Cloud credentials](grafana-cloud-credentials.md).

## Working on this repository

```bash
just setup     # Python workspace, Node workspace, Terraform providers
just check     # the whole gate: fmt, lint, test, IaC validate, conformance
just package generic-s3
```

Read [Adding an example](adding-an-example.md) before creating a new one, and
[Architecture](architecture.md) before changing the packaging, release or conformance machinery.

## Licence

Apache-2.0. Source and issues: [GitHub](https://github.com/rknightion/grafana-cloud-reference-examples).
