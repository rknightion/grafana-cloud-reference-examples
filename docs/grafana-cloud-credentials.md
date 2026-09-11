# Grafana Cloud credentials

Read this before wiring auth in an example, or when a push is returning 401.

Every example needs three things. Two are configuration; one is a secret.

| What | Where it comes from | Where it goes |
| --- | --- | --- |
| Endpoint | Stack details page | Environment variable |
| Tenant id | Stack details page | Environment variable |
| Token | A Cloud Access Policy you create | Secrets Manager |

## Finding the endpoint and the tenant id

In Grafana Cloud, open your stack, then the details page for the relevant
service - **Loki** for logs, **Prometheus** for metrics, **Tempo** for traces.
Each shows a URL and a numeric **user** or **instance id**.

The numeric id is the tenant id. **It is different for each service in the same
stack**, so a Loki tenant id will not authenticate against Prometheus. The most
common cause of a 401 that looks like a bad token is the wrong service's id.

The endpoint is shown in several forms depending on where you look. All of these
are accepted; they are normalised to the push URL:

```
logs-prod-012.grafana.net
https://logs-prod-012.grafana.net
https://logs-prod-012.grafana.net/loki/api/v1/push
```

A URL whose path is neither empty nor a Loki push path is **rejected** rather
than guessed at, because silently rewriting a Prometheus remote-write URL into a
Loki push URL produces a 404 that reads like an auth problem.

## Creating the token

A **Cloud Access Policy** token, from **Access Policies** in your Grafana Cloud
account (not a Grafana service account token, and not an API key - those
authenticate to a different surface).

Scope it to exactly what the example writes, and no more:

| Example writes to | Scope |
| --- | --- |
| Loki | `logs:write` |
| Mimir | `metrics:write` |
| Tempo | `traces:write` |
| Profiles | `profiles:write` |

A token with `logs:write` cannot read logs back, which is the correct blast
radius for a shipper. Set an expiry if your policy requires one, and note that
the function picks up a rotated token within its cache TTL (15 minutes by
default) without a redeploy.

## Storing the token

Create the secret yourself, before deploying. Neither IaC path takes the token as
an input.

Pass it as a file, not inline: an inline `--secret-string` puts the token in your
shell history and, briefly, in the process list where any other user on the box
can read it.

```bash
umask 077 && cat > /tmp/gc-loki.json <<'JSON'
{"tenant_id":"123456","token":"glc_eyJ..."}
JSON
aws secretsmanager create-secret \
  --name grafana-cloud/loki \
  --secret-string file:///tmp/gc-loki.json
rm -f /tmp/gc-loki.json
```

A bare token string is also accepted, because that is what you get from pasting
into the Secrets Manager console without choosing key/value. Same rule about the
file:

```bash
umask 077 && printf '%s' 'glc_eyJ...' > /tmp/gc-loki.txt
aws secretsmanager create-secret --name grafana-cloud/loki --secret-string file:///tmp/gc-loki.txt
rm -f /tmp/gc-loki.txt
```

Inside a JSON secret, the token is read from the first of `token`, `loki_token`,
`password`, `api_key` or `grafana_cloud_token` that is present, and the tenant id
from the first of `tenant_id`, `username`, `user`, `instance_id` or `loki_user`.
Several spellings are accepted because customers store this alongside credentials
for other tools and the naming is never consistent.

## Why the token is never an IaC input

A Terraform variable holding a token is written to state in plaintext, appears in
any shared plan output, and shows up in CI logs on a verbose run.

A CloudFormation parameter is not much better. `NoEcho` genuinely does mask the
value in the console, the CLI and the API, `DescribeStacks` and
`DescribeStackEvents` included. What it does not do is redact that value once it
reaches somewhere else: an Output, a resource property, a resource identifier, or
template metadata. And the value still has to travel through whatever invoked the
deploy, which is usually CI.

A Lambda environment variable is no better: it is readable by anyone with
`lambda:GetFunctionConfiguration`, and Terraform stores it in state.

So the IaC takes the secret's **name or ARN**, grants the function role
`secretsmanager:GetSecretValue` on that one secret, and the function fetches the
token at cold start. `just lint` fails on any template with a credential-shaped
parameter that has a default, or a credential-shaped environment variable.

## Rotating

Update the secret. Warm containers pick the new value up within the cache TTL
(`DEFAULT_CACHE_TTL_SECONDS`, 15 minutes), and a 401 triggers an immediate
re-fetch and one retry - so a rotation mid-flight recovers on its own.

A second 401 after a refresh is treated as permanent and not retried, because a
wrong scope on the access policy never recovers and retrying it burns the whole
retry budget.

## Diagnosing a 401 or 403

In this order:

1. **Wrong service's tenant id.** Check you took it from the Loki page, not
   Prometheus. This is the most common cause.
2. **Missing scope.** The policy needs `logs:write`. Reading the access policy is
   quicker than reading the function's logs.
3. **Token expired or revoked.** Check the access policy's token list.
4. **Wrong region's endpoint.** An endpoint from a different stack authenticates
   against a tenant that does not exist there.
5. **The role cannot read the secret.** A failure here says *Secrets Manager*,
   not Loki. If the secret uses a customer-managed KMS key the role also needs
   `kms:Decrypt`; without it the error names Secrets Manager and sends you to the
   wrong policy.
