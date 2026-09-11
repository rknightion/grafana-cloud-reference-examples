# Grafana Cloud reference examples

Working examples of getting data into Grafana Cloud, and of wiring Grafana Cloud up to other
products. Scripts, Lambda functions, containers, whatever the job needs.

Each example is a self-contained deliverable: download one zip, read one README, deploy with either
Terraform or CloudFormation. They are reference implementations meant to be read, forked and
adapted, not a supported product.

## Examples

| Example | What it does | Runtime | Status |
| --- | --- | --- | --- |
| [`generic-s3`](examples/generic-s3) | Ships arbitrary files landing in an S3 bucket to Grafana Cloud Loki | `python3.14` | alpha |
| [`adobe-aem`](examples/adobe-aem) | Ships Adobe Experience Manager Cloud Service logs from S3 to Loki with parsed labels | `python3.14` | planned |

`just examples` prints this from the manifests, which are the source of truth.

## Using an example

Grab the latest zip for the example you want from
[Releases](https://github.com/rknightion/grafana-cloud-reference-examples/releases). Tags are
per-example, so `generic-s3-v1.2.0` only ever changes `generic-s3`.

The zip contains the function code, the shared library it needs, a Terraform root module and an
equivalent single-file CloudFormation template. Pick one; they deploy the same thing.

```
generic-s3-v1.2.0.zip
├── README.md
├── lambda.zip              ready to upload, or let the IaC upload it
├── terraform/              root module + vendored modules/, no backend block
└── cloudformation/         one flat template.yaml + parameters.example.json
```

You supply a Grafana Cloud tenant id and a Cloud Access Policy token with the `logs:write` scope.
See [`docs/grafana-cloud-credentials.md`](docs/grafana-cloud-credentials.md).

## Working on this repo

```bash
just setup     # Python workspace, Node workspace, Terraform providers
just check     # the whole gate: fmt, lint, test, IaC validate, conformance
just package generic-s3
```

`just --list` shows everything. `AGENTS.md` (symlinked as `CLAUDE.md`) holds the conventions.

## Documentation

| Read this | Before |
| --- | --- |
| [`docs/adding-an-example.md`](docs/adding-an-example.md) | Creating a new example |
| [`docs/grafana-cloud-credentials.md`](docs/grafana-cloud-credentials.md) | Wiring auth, or diagnosing a 401 |
| [`docs/loki-ingestion.md`](docs/loki-ingestion.md) | Choosing labels, timestamps or batch sizes |
| [`docs/runtimes.md`](docs/runtimes.md) | Choosing or bumping a Lambda runtime |
| [`docs/otlp-vs-loki-push.md`](docs/otlp-vs-loki-push.md) | Proposing OTLP as an output |
| [`docs/architecture.md`](docs/architecture.md) | Changing the packaging, release or conformance machinery |

The shared pieces document themselves too:
[`common/python`](common/python), [`common/nodejs`](common/nodejs),
[`common/go`](common/go), [`common/terraform/modules`](common/terraform/modules),
[`common/cloudformation`](common/cloudformation).

## Licence

Apache 2.0. See [LICENSE](LICENSE).
