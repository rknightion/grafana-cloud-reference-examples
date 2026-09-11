import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { configFromEnv, normalisePushUrl } from '../src/config.js';
import { ConfigError } from '../src/errors.js';

const REQUIRED = {
  GRAFANA_CLOUD_LOKI_ENDPOINT: 'logs-prod-012.grafana.net',
  GRAFANA_CLOUD_LOKI_TENANT_ID: '123456',
  GRAFANA_CLOUD_CREDENTIALS_SECRET_ID: 'grafana-cloud/loki',
};

const TOUCHED = [
  ...Object.keys(REQUIRED),
  'GRAFANA_CLOUD_LOKI_TOKEN',
  'LOKI_STATIC_LABELS',
  'LOKI_BATCH_MAX_LINES',
  'LOKI_COMPRESS',
];

describe('normalisePushUrl', () => {
  it.each([
    'logs-prod-012.grafana.net',
    'https://logs-prod-012.grafana.net',
    'https://logs-prod-012.grafana.net/',
    'https://logs-prod-012.grafana.net/loki',
    'https://logs-prod-012.grafana.net/loki/api/v1/push',
  ])('resolves %s to the push URL', (given) => {
    expect(normalisePushUrl(given)).toBe('https://logs-prod-012.grafana.net/loki/api/v1/push');
  });

  it('rejects plain http', () => {
    expect(() => normalisePushUrl('http://logs-prod-012.grafana.net')).toThrow(/must be https/);
  });

  it('rejects an unrecognised path rather than guessing', () => {
    expect(() =>
      normalisePushUrl('https://logs-prod-012.grafana.net/prometheus/api/v1/write'),
    ).toThrow(/not recognised/);
  });

  it('rejects an empty endpoint', () => {
    expect(() => normalisePushUrl('   ')).toThrow(/empty/);
  });
});

describe('configFromEnv', () => {
  const saved = new Map<string, string | undefined>();

  beforeEach(() => {
    for (const key of TOUCHED) {
      saved.set(key, process.env[key]);
      delete process.env[key];
    }
    Object.assign(process.env, REQUIRED);
  });

  afterEach(() => {
    for (const [key, value] of saved) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  it('builds from the minimum set', () => {
    const config = configFromEnv();
    expect(config.tenantId).toBe('123456');
    expect(config.pushUrl.endsWith('/loki/api/v1/push')).toBe(true);
  });

  it('requires a credential source', () => {
    delete process.env['GRAFANA_CLOUD_CREDENTIALS_SECRET_ID'];
    expect(() => configFromEnv()).toThrow(ConfigError);
  });

  it('treats an empty string as unset', () => {
    // An empty Terraform variable or CloudFormation parameter renders as "",
    // which must fail loudly rather than producing a request to https:///.
    process.env['GRAFANA_CLOUD_LOKI_TENANT_ID'] = '  ';
    expect(() => configFromEnv()).toThrow(/required but unset or empty/);
  });

  it('rejects a non-numeric batch size', () => {
    process.env['LOKI_BATCH_MAX_LINES'] = 'lots';
    expect(() => configFromEnv()).toThrow(/must be an integer/);
  });

  it('rejects a zero batch size', () => {
    process.env['LOKI_BATCH_MAX_LINES'] = '0';
    expect(() => configFromEnv()).toThrow(/must be positive/);
  });

  it('parses static labels', () => {
    process.env['LOKI_STATIC_LABELS'] = '{"env":"prod","region":"eu-west-1"}';
    expect(configFromEnv().staticLabels).toEqual({ env: 'prod', region: 'eu-west-1' });
  });

  it('rejects static labels that are not an object', () => {
    process.env['LOKI_STATIC_LABELS'] = '["env","prod"]';
    expect(() => configFromEnv()).toThrow(/must be a JSON object/);
  });

  it.each([
    ['false', false],
    ['0', false],
    ['on', true],
  ])('parses LOKI_COMPRESS=%s', (given, expected) => {
    process.env['LOKI_COMPRESS'] = given;
    expect(configFromEnv().compress).toBe(expected);
  });
});
