# `grafana-cloud-common` (Python)

Shared building blocks for the Python reference examples: a Loki push client, a
batcher, structured logging, config loading, and AWS helpers for S3 and Secrets
Manager.

Not published anywhere. It is a uv workspace member, so examples import it
directly, and `just package <example>` vendors the source into the release zip.

## The dependency split, and why it is load-bearing

| Module | Depends on |
| --- | --- |
| `config`, `log`, `loki`, `batching`, `errors` | standard library only |
| `aws.s3`, `aws.credentials`, `aws.handler` | `boto3` |

An example that only needs the core ships a deployment package of a few tens of
kilobytes: fast to upload, fast to cold-start, and with no third-party CVE
surface to patch. A test asserts the core imports cleanly with `boto3` absent, so
this does not rot.

Do not add `requests`, `urllib3` or a protobuf/snappy stack to the core. The
`urllib`-based JSON push path is deliberate; `docs/loki-ingestion.md` says when
the protobuf path is worth its dependencies.

## Typical handler shape

```python
from grafana_cloud_common import LokiClient, LokiConfig, LogEntry, StreamBatcher, configure
from grafana_cloud_common.aws import (
    CredentialProvider,
    S3ObjectReader,
    parse_event,
    process_messages,
)

configure()
CONFIG = LokiConfig.from_env()
CREDENTIALS = CredentialProvider(CONFIG.credentials_secret_id, static_token=CONFIG.token)
CLIENT = LokiClient(CONFIG, CREDENTIALS.token)
READER = S3ObjectReader()
```

Build that at module scope, not inside the handler: it runs once per execution
environment instead of once per invocation, and a bad config then fails the first
invocation loudly rather than degrading quietly.
