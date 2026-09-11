import { describe, expect, it } from 'vitest';
import { gunzipSync } from 'node:zlib';

import type { LokiConfig } from '../src/config.js';
import { PermanentError, RetryableError } from '../src/errors.js';
import {
  entry,
  groupByLabels,
  LokiClient,
  validateLabels,
  type FetchLike,
  type LokiRequestInit,
} from '../src/loki.js';

function config(overrides: Partial<LokiConfig> = {}): LokiConfig {
  return {
    pushUrl: 'https://logs-prod-012.grafana.net/loki/api/v1/push',
    tenantId: '123456',
    credentialsSecretId: undefined,
    token: 'glc_fake',
    staticLabels: {},
    batchMaxLines: 3,
    batchMaxBytes: 1_000_000,
    requestTimeoutSeconds: 1,
    maxRetries: 3,
    compress: false,
    ...overrides,
  };
}

interface Recorded {
  url: string;
  init: LokiRequestInit;
}

/**
 * Scripted responses, consumed in order. An entry that is a number is a status
 * code; an Error is thrown. Deliberately hand-rolled rather than a fetch mock.
 */
function scriptedFetch(
  script: Array<number | Error | { status: number; headers: Record<string, string>; body: string }>,
): { fetchImpl: FetchLike; recorded: Recorded[] } {
  const recorded: Recorded[] = [];
  const fetchImpl: FetchLike = (url, init) => {
    recorded.push({ url, init });
    const next = script.shift();
    if (next === undefined) throw new Error('scripted fetch ran out of responses');
    if (next instanceof Error) return Promise.reject(next);
    if (typeof next === 'number') {
      return Promise.resolve(new Response(null, { status: next }));
    }
    return Promise.resolve(new Response(next.body, { status: next.status, headers: next.headers }));
  };
  return { fetchImpl, recorded };
}

const LABELS = { service_name: 'test' };

type PushValue = [string, string] | [string, string, Record<string, string>];

interface PushBody {
  streams: Array<{ stream: Record<string, string>; values: PushValue[] }>;
}

/** JSON.parse returns `any`; assert the shape once rather than at every use. */
function parsePayload(payload: Buffer | Uint8Array): PushBody {
  return JSON.parse(Buffer.from(payload).toString('utf8')) as PushBody;
}

describe('validateLabels', () => {
  it('accepts a low-cardinality set', () => {
    expect(() => validateLabels({ service_name: 'generic-s3', bucket: 'acme-logs' })).not.toThrow();
  });

  it('rejects an empty set', () => {
    expect(() => validateLabels({})).toThrow(/at least one label/);
  });

  it.each(['object_key', 'request_id', 'filename', 'ID', 'TraceID'])(
    'rejects the high-cardinality name %s',
    (name) => {
      expect(() => validateLabels({ [name]: 'anything' })).toThrow(/high-cardinality/);
    },
  );

  it.each(['9lives', 'has-a-dash', 'has.a.dot'])('rejects the invalid name %s', (name) => {
    expect(() => validateLabels({ [name]: 'value' })).toThrow(/not valid Loki syntax/);
  });

  it('rejects an empty value', () => {
    expect(() => validateLabels({ service_name: '' })).toThrow(/empty value/);
  });

  it('rejects too many labels', () => {
    const labels = Object.fromEntries(
      Array.from({ length: 20 }, (_, index) => [`label_${index}`, 'v']),
    );
    expect(() => validateLabels(labels)).toThrow(/label ceiling/);
  });
});

describe('buildPayload', () => {
  it('sorts entries ascending within a stream', () => {
    const client = new LokiClient(config(), () => 'glc_fake', scriptedFetch([]).fetchImpl);
    const payload = parsePayload(
      client.buildPayload([
        [
          LABELS,
          [
            entry('third', undefined, 300n),
            entry('first', undefined, 100n),
            entry('second', undefined, 200n),
          ],
        ],
      ]),
    );
    expect(payload.streams[0]?.values.map((value) => value[0])).toEqual(['100', '200', '300']);
    expect(payload.streams[0]?.values.map((value) => value[1])).toEqual([
      'first',
      'second',
      'third',
    ]);
  });

  it('omits the metadata slot when there is none', () => {
    const client = new LokiClient(config(), () => 'glc_fake', scriptedFetch([]).fetchImpl);
    const payload = parsePayload(client.buildPayload([[LABELS, [entry('a', undefined, 1n)]]]));
    expect(payload.streams[0]?.values).toEqual([['1', 'a']]);
  });

  it('emits structured metadata as the third slot', () => {
    const client = new LokiClient(config(), () => 'glc_fake', scriptedFetch([]).fetchImpl);
    const payload = parsePayload(
      client.buildPayload([[LABELS, [entry('a', { object_key: 'logs/a.gz' }, 1n)]]]),
    );
    expect(payload.streams[0]?.values).toEqual([['1', 'a', { object_key: 'logs/a.gz' }]]);
  });

  it('keeps nanosecond precision, which a JS number could not', () => {
    const client = new LokiClient(config(), () => 'glc_fake', scriptedFetch([]).fetchImpl);
    const exact = 1757548800123456789n;
    const payload = parsePayload(client.buildPayload([[LABELS, [entry('a', undefined, exact)]]]));
    expect(payload.streams[0]?.values[0]?.[0]).toBe('1757548800123456789');
  });

  it('merges streams with identical labels regardless of key order', () => {
    const client = new LokiClient(config(), () => 'glc_fake', scriptedFetch([]).fetchImpl);
    const payload = parsePayload(
      client.buildPayload([
        [{ a: '1', b: '2' }, [entry('first', undefined, 1n)]],
        [{ b: '2', a: '1' }, [entry('second', undefined, 2n)]],
      ]),
    );
    expect(payload.streams).toHaveLength(1);
    expect(payload.streams[0]?.values).toHaveLength(2);
  });

  it('merges static labels and lets a stream override them', () => {
    const client = new LokiClient(
      config({ staticLabels: { env: 'prod', service_name: 'default' } }),
      () => 'glc_fake',
      scriptedFetch([]).fetchImpl,
    );
    const payload = parsePayload(
      client.buildPayload([[{ service_name: 'override' }, [entry('a', undefined, 1n)]]]),
    );
    expect(payload.streams[0]?.stream).toEqual({ env: 'prod', service_name: 'override' });
  });
});

describe('push', () => {
  it('returns the line count on success', async () => {
    const { fetchImpl, recorded } = scriptedFetch([204]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).resolves.toBe(1);
    expect(recorded[0]?.init.headers['authorization']).toMatch(/^Basic /);
  });

  it('does nothing for an empty batch', async () => {
    const { fetchImpl, recorded } = scriptedFetch([]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([])).resolves.toBe(0);
    expect(recorded).toHaveLength(0);
  });

  it('gzips when configured', async () => {
    const { fetchImpl, recorded } = scriptedFetch([204]);
    const client = new LokiClient(config({ compress: true }), () => 'glc_fake', fetchImpl);
    await client.push([[LABELS, [entry('a', undefined, 1n)]]]);
    expect(recorded[0]?.init.headers['content-encoding']).toBe('gzip');
    const body = recorded[0]?.init.body;
    expect(body).toBeDefined();
    expect(parsePayload(gunzipSync(body as Uint8Array)).streams).toHaveLength(1);
  });

  it('retries a 429 then succeeds', async () => {
    const { fetchImpl, recorded } = scriptedFetch([
      { status: 429, headers: { 'retry-after': '0' }, body: 'slow down' },
      204,
    ]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).resolves.toBe(1);
    expect(recorded).toHaveLength(2);
  });

  it('gives up after the retry budget', async () => {
    const { fetchImpl, recorded } = scriptedFetch([
      { status: 503, headers: { 'retry-after': '0' }, body: 'down' },
      { status: 503, headers: { 'retry-after': '0' }, body: 'down' },
      { status: 503, headers: { 'retry-after': '0' }, body: 'down' },
    ]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).rejects.toThrow(
      RetryableError,
    );
    expect(recorded).toHaveLength(3);
  });

  it('treats a 400 as permanent and does not retry', async () => {
    const { fetchImpl, recorded } = scriptedFetch([
      { status: 400, headers: {}, body: 'entry out of order' },
    ]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).rejects.toThrow(
      PermanentError,
    );
    expect(recorded).toHaveLength(1);
  });

  it('refreshes the credential once on a 401 then gives up', async () => {
    const tokens = ['stale', 'fresh'];
    const { fetchImpl, recorded } = scriptedFetch([
      { status: 401, headers: {}, body: 'no' },
      { status: 401, headers: {}, body: 'no' },
    ]);
    const client = new LokiClient(config(), () => tokens.shift() ?? 'exhausted', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).rejects.toThrow(
      /logs:write/,
    );
    expect(recorded).toHaveLength(2);
    expect(recorded[0]?.init.headers['authorization']).not.toBe(
      recorded[1]?.init.headers['authorization'],
    );
  });

  it('rejects a banned label before making any request', async () => {
    const { fetchImpl, recorded } = scriptedFetch([]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(
      client.push([[{ object_key: 'a/b.gz' }, [entry('a', undefined, 1n)]]]),
    ).rejects.toThrow(/high-cardinality/);
    expect(recorded).toHaveLength(0);
  });

  it('retries a transport failure', async () => {
    const { fetchImpl, recorded } = scriptedFetch([new Error('socket hang up'), 204]);
    const client = new LokiClient(config(), () => 'glc_fake', fetchImpl);
    await expect(client.push([[LABELS, [entry('a', undefined, 1n)]]])).resolves.toBe(1);
    expect(recorded).toHaveLength(2);
  });
});

describe('groupByLabels', () => {
  it('collapses pairs into streams', () => {
    const grouped = groupByLabels([
      [{ service_name: 'a' }, entry('one', undefined, 1n)],
      [{ service_name: 'b' }, entry('two', undefined, 2n)],
      [{ service_name: 'a' }, entry('three', undefined, 3n)],
    ]);
    expect(grouped).toHaveLength(2);
    const byService = new Map(
      grouped.map(([labels, entries]) => [labels['service_name'], entries]),
    );
    expect(byService.get('a')?.map((e) => e.line)).toEqual(['one', 'three']);
    expect(byService.get('b')?.map((e) => e.line)).toEqual(['two']);
  });
});
