/**
 * Resolve the Grafana Cloud credential from Secrets Manager.
 *
 * Fetched once per execution environment and cached, because a GetSecretValue on
 * every invocation is a per-invocation cost, a per-invocation latency and a
 * throttling risk at any real concurrency.
 *
 * Cached with a TTL rather than forever, so a rotated token is picked up by a
 * warm container within the TTL rather than at its next cold start - which under
 * steady traffic may be hours away.
 */

import {
  GetSecretValueCommand,
  SecretsManagerClient,
  type GetSecretValueCommandOutput,
  type SecretsManagerClientConfig,
} from '@aws-sdk/client-secrets-manager';

import { ConfigError, PermanentError, RetryableError } from '../errors.js';
import { getLogger } from '../logger.js';

const LOG = getLogger('aws.credentials');

export const DEFAULT_CACHE_TTL_SECONDS = 900;

/**
 * Keys accepted inside a JSON secret, in preference order. Several are supported
 * because customers store this alongside credentials for other tools and the
 * naming is never consistent.
 */
const TOKEN_KEYS = ['token', 'loki_token', 'password', 'api_key', 'grafana_cloud_token'];
const TENANT_KEYS = ['tenant_id', 'username', 'user', 'instance_id', 'loki_user'];

/**
 * Secrets Manager models only some failures as named error classes.
 * AccessDeniedException and ThrottlingException are NOT among them; they arrive
 * with those names on a generic error, so dispatch on the name rather than on an
 * `instanceof` against a class that does not exist.
 */
const PERMANENT_CODES = new Set([
  'ResourceNotFoundException',
  'InvalidRequestException',
  'InvalidParameterException',
  'AccessDeniedException',
  'UnrecognizedClientException',
]);
const RETRYABLE_CODES = new Set([
  'InternalServiceError',
  'ThrottlingException',
  'LimitExceededException',
]);

export interface GrafanaCloudCredential {
  readonly token: string;
  readonly tenantId?: string;
}

/**
 * Parse a secret that is either a bare token or a JSON object.
 *
 * A bare string is accepted because that is what a customer produces when they
 * paste a token into the Secrets Manager console without choosing key/value.
 */
export function parseSecret(payload: string): GrafanaCloudCredential {
  const stripped = payload.trim();
  if (stripped === '') throw new ConfigError('the credentials secret is empty');

  // A bare token can never start with a JSON opener, so anything that does is
  // meant to be JSON and a parse failure is a real error.
  if (!stripped.startsWith('{') && !stripped.startsWith('[')) return { token: stripped };

  let decoded: unknown;
  try {
    decoded = JSON.parse(stripped);
  } catch (cause) {
    throw new ConfigError(`credentials secret is neither a bare token nor JSON: ${String(cause)}`);
  }
  if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) {
    throw new ConfigError('credentials secret JSON must be an object');
  }

  const record = decoded as Record<string, unknown>;
  const tokenKey = TOKEN_KEYS.find((key) => typeof record[key] === 'string' && record[key] !== '');
  if (tokenKey === undefined) {
    throw new ConfigError(
      `credentials secret JSON has no token. Expected one of: ${TOKEN_KEYS.join(', ')}`,
    );
  }
  const tenantKey = TENANT_KEYS.find(
    (key) => record[key] !== undefined && record[key] !== null && record[key] !== '',
  );
  return {
    token: String(record[tokenKey]),
    ...(tenantKey ? { tenantId: String(record[tenantKey]) } : {}),
  };
}

/**
 * Just the one call this makes. Narrower than the SDK client so a test double is
 * a few lines, and so the SDK's overloaded `send` signature does not leak into
 * every call site.
 */
export interface SecretsSender {
  send(command: GetSecretValueCommand): Promise<GetSecretValueCommandOutput>;
}

export interface CredentialProviderOptions {
  readonly staticToken?: string;
  readonly ttlSeconds?: number;
  readonly client?: SecretsSender;
  readonly clientConfig?: SecretsManagerClientConfig;
}

/**
 * Caching resolver for the Grafana Cloud credential.
 *
 * Pass `provider.token` as LokiClient's tokenProvider; the client calls it again
 * after a 401, so a rotated token is picked up without coordination.
 */
export class CredentialProvider {
  private readonly secretId: string | undefined;
  private readonly staticToken: string | undefined;
  private readonly ttlSeconds: number;
  private readonly clientConfig: SecretsManagerClientConfig;
  private client: SecretsSender | undefined;
  private cached: GrafanaCloudCredential | undefined;
  private expiresAt = 0;

  constructor(secretId: string | undefined, options: CredentialProviderOptions = {}) {
    if (secretId === undefined && options.staticToken === undefined) {
      throw new ConfigError('a secret id or a static token is required');
    }
    this.secretId = secretId;
    this.staticToken = options.staticToken;
    this.ttlSeconds = options.ttlSeconds ?? DEFAULT_CACHE_TTL_SECONDS;
    this.client = options.client;
    this.clientConfig = options.clientConfig ?? {};
  }

  private secretsManager(): SecretsSender {
    // Created lazily so a unit test never needs AWS credentials, and a
    // static-token deployment makes no client at all.
    this.client ??= new SecretsManagerClient(this.clientConfig);
    return this.client;
  }

  /** Token-only accessor, shaped for LokiClient's tokenProvider parameter. */
  token = async (): Promise<string> => (await this.resolve()).token;

  async resolve(options: { force?: boolean } = {}): Promise<GrafanaCloudCredential> {
    const now = Date.now();
    if (!options.force && this.cached !== undefined && now < this.expiresAt) return this.cached;

    const credential =
      this.secretId === undefined
        ? { token: this.staticToken as string }
        : parseSecret(await this.fetch(this.secretId));

    this.cached = credential;
    this.expiresAt = now + this.ttlSeconds * 1000;
    return credential;
  }

  /** Drop the cache so the next resolve() refetches. */
  invalidate(): void {
    this.cached = undefined;
    this.expiresAt = 0;
  }

  private async fetch(secretId: string): Promise<string> {
    let response: GetSecretValueCommandOutput;
    try {
      response = await this.secretsManager().send(
        new GetSecretValueCommand({ SecretId: secretId }),
      );
    } catch (cause) {
      throw classify(cause, secretId);
    }

    if (response.SecretString !== undefined) {
      LOG.debug('resolved grafana cloud credential', { secret_id: secretId });
      return response.SecretString;
    }
    if (response.SecretBinary !== undefined) {
      return Buffer.from(response.SecretBinary).toString('utf8');
    }
    throw new ConfigError(`secret "${secretId}" has neither a string nor a binary value`);
  }
}

function classify(cause: unknown, secretId: string): Error {
  const code = (cause as { name?: string }).name ?? '';
  if (code === 'ResourceNotFoundException') {
    return new PermanentError(`secret "${secretId}" does not exist`);
  }
  if (code === 'AccessDeniedException' || code === 'UnrecognizedClientException') {
    return new PermanentError(
      `not permitted to read secret "${secretId}". The function role needs ` +
        `secretsmanager:GetSecretValue on it, and kms:Decrypt on its key if it uses a ` +
        `customer-managed key.`,
    );
  }
  if (RETRYABLE_CODES.has(code)) {
    return new RetryableError(`Secrets Manager unavailable for "${secretId}": ${code}`);
  }
  if (PERMANENT_CODES.has(code)) {
    return new PermanentError(`Secrets Manager rejected "${secretId}": ${code}`);
  }
  return new RetryableError(`Secrets Manager error for "${secretId}": ${code || String(cause)}`);
}
