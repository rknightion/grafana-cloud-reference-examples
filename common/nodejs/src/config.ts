/**
 * Environment-driven configuration, validated once at cold start.
 *
 * Environment variable names, defaults and failure messages are identical to the
 * Python library's, so a customer who has deployed a Python example can read a
 * Node one without relearning them.
 */

import { ConfigError } from './errors.js';

export const DEFAULT_BATCH_MAX_BYTES = 4 * 1024 * 1024;
export const DEFAULT_BATCH_MAX_LINES = 5_000;
export const DEFAULT_REQUEST_TIMEOUT_SECONDS = 20;
export const DEFAULT_MAX_RETRIES = 5;

const PUSH_PATH = '/loki/api/v1/push';

export interface LokiConfig {
  readonly pushUrl: string;
  readonly tenantId: string;
  readonly credentialsSecretId: string | undefined;
  readonly token: string | undefined;
  readonly staticLabels: Readonly<Record<string, string>>;
  readonly batchMaxLines: number;
  readonly batchMaxBytes: number;
  readonly requestTimeoutSeconds: number;
  readonly maxRetries: number;
  readonly compress: boolean;
}

function env(name: string): string | undefined {
  const value = process.env[name]?.trim();
  return value === '' ? undefined : value;
}

function required(name: string): string {
  const value = env(name);
  if (value === undefined) throw new ConfigError(`${name} is required but unset or empty`);
  return value;
}

function positiveInt(name: string, fallback: number): number {
  const raw = env(name);
  if (raw === undefined) return fallback;
  const parsed = Number(raw);
  if (!Number.isInteger(parsed)) throw new ConfigError(`${name} must be an integer, got "${raw}"`);
  if (parsed <= 0) throw new ConfigError(`${name} must be positive, got ${parsed}`);
  return parsed;
}

function positiveNumber(name: string, fallback: number): number {
  const raw = env(name);
  if (raw === undefined) return fallback;
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) throw new ConfigError(`${name} must be a number, got "${raw}"`);
  if (parsed <= 0) throw new ConfigError(`${name} must be positive, got ${parsed}`);
  return parsed;
}

function boolean_(name: string, fallback: boolean): boolean {
  const raw = env(name)?.toLowerCase();
  if (raw === undefined) return fallback;
  if (['1', 'true', 'yes', 'on'].includes(raw)) return true;
  if (['0', 'false', 'no', 'off'].includes(raw)) return false;
  throw new ConfigError(`${name} must be a boolean, got "${raw}"`);
}

/**
 * Accept anything a customer is likely to paste and return the push URL.
 *
 * The Grafana Cloud UI shows several forms of the same thing. All are normalised
 * here rather than at every call site, because getting this wrong produces a 404
 * that reads like an auth problem.
 */
export function normalisePushUrl(endpoint: string): string {
  let candidate = endpoint.trim();
  if (candidate === '') throw new ConfigError('Loki endpoint is empty');
  if (!candidate.includes('://')) candidate = `https://${candidate}`;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new ConfigError(`Loki endpoint is not a valid URL: "${endpoint}"`);
  }
  if (parsed.protocol !== 'https:') {
    throw new ConfigError(`Loki endpoint must be https, got "${parsed.protocol}"`);
  }

  const path = parsed.pathname.replace(/\/+$/, '');
  let resolved: string;
  if (path.endsWith(PUSH_PATH)) {
    resolved = path;
  } else if (path === '') {
    resolved = PUSH_PATH;
  } else if (path.endsWith('/loki')) {
    resolved = `${path}/api/v1/push`;
  } else {
    throw new ConfigError(
      `Loki endpoint path "${parsed.pathname}" is not recognised. Pass the host, ` +
        `the base URL, or a URL ending in ${PUSH_PATH}.`,
    );
  }

  return `https://${parsed.host}${resolved}`;
}

function parseStaticLabels(raw: string | undefined): Record<string, string> {
  if (raw === undefined) return {};
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch (cause) {
    throw new ConfigError(`LOKI_STATIC_LABELS must be a JSON object: ${String(cause)}`);
  }
  if (typeof decoded !== 'object' || decoded === null || Array.isArray(decoded)) {
    throw new ConfigError('LOKI_STATIC_LABELS must be a JSON object');
  }
  const labels: Record<string, string> = {};
  for (const [key, value] of Object.entries(decoded)) {
    if (typeof value !== 'string') {
      throw new ConfigError('LOKI_STATIC_LABELS keys and values must both be strings');
    }
    labels[key] = value;
  }
  return labels;
}

/** Build from environment variables, throwing ConfigError on anything wrong. */
export function configFromEnv(): LokiConfig {
  const credentialsSecretId = env('GRAFANA_CLOUD_CREDENTIALS_SECRET_ID');
  const token = env('GRAFANA_CLOUD_LOKI_TOKEN');
  if (credentialsSecretId === undefined && token === undefined) {
    throw new ConfigError(
      'Set GRAFANA_CLOUD_CREDENTIALS_SECRET_ID (the deployed path) or ' +
        'GRAFANA_CLOUD_LOKI_TOKEN (local testing only)',
    );
  }

  return {
    pushUrl: normalisePushUrl(required('GRAFANA_CLOUD_LOKI_ENDPOINT')),
    tenantId: required('GRAFANA_CLOUD_LOKI_TENANT_ID'),
    credentialsSecretId,
    token,
    staticLabels: parseStaticLabels(env('LOKI_STATIC_LABELS')),
    batchMaxLines: positiveInt('LOKI_BATCH_MAX_LINES', DEFAULT_BATCH_MAX_LINES),
    batchMaxBytes: positiveInt('LOKI_BATCH_MAX_BYTES', DEFAULT_BATCH_MAX_BYTES),
    requestTimeoutSeconds: positiveNumber(
      'LOKI_REQUEST_TIMEOUT_SECONDS',
      DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ),
    maxRetries: positiveInt('LOKI_MAX_RETRIES', DEFAULT_MAX_RETRIES),
    compress: boolean_('LOKI_COMPRESS', true),
  };
}
