import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { Logger, redact } from '../src/logger.js';

function captureStdout(): { lines: string[]; restore: () => void } {
  const lines: string[] = [];
  const spy = vi
    .spyOn(process.stdout, 'write')
    .mockImplementation((chunk: string | Uint8Array): boolean => {
      lines.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'));
      return true;
    });
  return { lines, restore: () => spy.mockRestore() };
}

describe('redact', () => {
  it.each([
    'token',
    'GRAFANA_CLOUD_TOKEN',
    'password',
    'authorization',
    'apiKey',
    'api_key',
    'credential',
  ])('masks the value of %s', (key) => {
    expect(redact({ [key]: 'glc_secret' })[key]).toBe('***redacted***');
  });

  it('leaves an ordinary field alone', () => {
    expect(redact({ bucket: 'acme-logs' })).toEqual({ bucket: 'acme-logs' });
  });
});

describe('Logger', () => {
  const saved = process.env['LOG_LEVEL'];

  beforeEach(() => {
    delete process.env['LOG_LEVEL'];
  });

  afterEach(() => {
    if (saved === undefined) delete process.env['LOG_LEVEL'];
    else process.env['LOG_LEVEL'] = saved;
  });

  it('emits one JSON object per line', () => {
    const { lines, restore } = captureStdout();
    try {
      new Logger('test').info('hello', { lines: 3 });
    } finally {
      restore();
    }
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0] as string) as Record<string, unknown>;
    expect(parsed['level']).toBe('INFO');
    expect(parsed['message']).toBe('hello');
    expect(parsed['lines']).toBe(3);
  });

  it('suppresses a line below the threshold', () => {
    const { lines, restore } = captureStdout();
    try {
      new Logger('test', {}, 'WARNING').info('hidden');
    } finally {
      restore();
    }
    expect(lines).toEqual([]);
  });

  it('carries an explicit level through bind()', () => {
    // Without this, binding a field on a logger built with an explicit level
    // silently reverts it to whatever LOG_LEVEL says.
    process.env['LOG_LEVEL'] = 'DEBUG';
    const { lines, restore } = captureStdout();
    try {
      new Logger('test', {}, 'ERROR').bind({ request_id: 'r-1' }).info('should not appear');
    } finally {
      restore();
    }
    expect(lines).toEqual([]);
  });

  it('carries bound fields through to every line', () => {
    const { lines, restore } = captureStdout();
    try {
      new Logger('test').bind({ request_id: 'r-1' }).warning('careful');
    } finally {
      restore();
    }
    const parsed = JSON.parse(lines[0] as string) as Record<string, unknown>;
    expect(parsed['request_id']).toBe('r-1');
  });

  it('redacts a credential-shaped field on the way out', () => {
    const { lines, restore } = captureStdout();
    try {
      new Logger('test').info('auth', { token: 'glc_secret' });
    } finally {
      restore();
    }
    expect(lines[0]).not.toContain('glc_secret');
  });
});
