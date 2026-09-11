# `@grafana-cloud/common` (Node.js)

Shared building blocks for the Node.js reference examples: a Loki push client, a
batcher, structured logging, config loading, and AWS helpers for S3 and Secrets
Manager. A faithful counterpart to `common/python` - same environment variable
names, same defaults, same error messages - so a customer who has deployed a
Python example can read a Node one without relearning anything.

Not published anywhere. It is an npm workspace member, and `just package
<example>` vendors the built output into the release zip.

## The dependency split, and why it is load-bearing

| Entry point | Depends on |
| --- | --- |
| `@grafana-cloud/common` | global `fetch` and `node:zlib` only |
| `@grafana-cloud/common/aws` | `@aws-sdk/client-s3`, `@aws-sdk/client-secrets-manager` |

The SDK clients are **peer** dependencies, because the Node.js Lambda runtime
provides AWS SDK v3. A test asserts statically that no core module imports
`@aws-sdk`, including a type-only import - which erases at runtime and would
pass a module-registry check while still being wrong.

Do not add `axios`, `node-fetch` or `undici` to the core. Node 24 has `fetch`.

## Two things that will bite you

**Timestamps are `bigint`, not `number`.** A nanosecond epoch is 19 digits and a
double carries about 16, so a `number` timestamp silently rounds to the nearest
few hundred nanoseconds. `nowNs()` returns a `bigint` and `LogEntry.timestampNs`
requires one.

**`fetch` has no timeout option.** The client passes
`AbortSignal.timeout(...)`; a Lambda that hangs on a socket otherwise burns its
whole configured duration and gets killed without returning a
partial-failure response.

## Typical handler shape

```ts
import { LokiClient, StreamBatcher, configFromEnv, entry, getLogger } from '@grafana-cloud/common';
import { CredentialProvider, S3ObjectReader, parseEvent, processMessages, toResponse } from '@grafana-cloud/common/aws';

const LOG = getLogger('handler');
const CONFIG = configFromEnv();
const CREDENTIALS = new CredentialProvider(CONFIG.credentialsSecretId, { staticToken: CONFIG.token });
const CLIENT = new LokiClient(CONFIG, CREDENTIALS.token);
const READER = new S3ObjectReader();
```

Build that at module scope, not inside the handler: it runs once per execution
environment instead of once per invocation, and a bad config then fails the
first invocation loudly rather than degrading quietly.
