/**
 * Grafana Cloud Loki push client.
 *
 * No runtime dependencies, on purpose: global `fetch` and `node:zlib` are both
 * in the Node 24 runtime, so an example that needs nothing but this module
 * produces a deployment package of a few tens of kilobytes with no third-party
 * CVE surface to patch. Resist adding axios or node-fetch.
 *
 * Uses the JSON push API rather than protobuf. The protobuf path is more
 * efficient at high throughput but needs snappy and generated stubs, and these
 * examples are read as much as they are run. See docs/loki-ingestion.md.
 */

import { gzipSync } from 'node:zlib';

import type { LokiConfig } from './config.js';
import { PermanentError, RetryableError } from './errors.js';
import { getLogger } from './logger.js';

const LOG = getLogger('loki');

/** Loki's own label-name grammar. A label failing this is rejected with a 400. */
const LABEL_NAME_RE = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

export const MAX_LABELS_PER_STREAM = 15;

/**
 * Label names that are a cardinality incident in every case we have seen. These
 * values belong in the log line or in structured metadata, never in a label.
 */
export const BANNED_LABEL_NAMES: ReadonlySet<string> = new Set([
  'request_id',
  'requestid',
  'trace_id',
  'traceid',
  'span_id',
  'spanid',
  'user_id',
  'userid',
  'customer_id',
  'customerid',
  'session_id',
  'sessionid',
  'object_key',
  'objectkey',
  's3_key',
  'key',
  'filename',
  'file_name',
  'path',
  'timestamp',
  'time',
  'date',
  'uuid',
  'id',
  'ip',
  'message',
  'line',
]);

const RETRYABLE_STATUSES: ReadonlySet<number> = new Set([408, 425, 429, 500, 502, 503, 504]);

export type Labels = Readonly<Record<string, string>>;

export interface LogEntry {
  /**
   * Epoch nanoseconds, as a bigint. A JS number cannot hold a nanosecond epoch
   * without losing precision - it is 19 digits and a double carries about 16 -
   * so a number here would silently round timestamps.
   */
  readonly timestampNs: bigint;
  readonly line: string;
  readonly structuredMetadata?: Readonly<Record<string, string>>;
}

/** One stream: its labels, and the entries belonging to it. */
export type Stream = readonly [Labels, readonly LogEntry[]];

export function nowNs(): bigint {
  return BigInt(Date.now()) * 1_000_000n;
}

export function entry(
  line: string,
  structuredMetadata?: Record<string, string>,
  timestampNs?: bigint,
): LogEntry {
  return {
    timestampNs: timestampNs ?? nowNs(),
    line,
    ...(structuredMetadata ? { structuredMetadata } : {}),
  };
}

/** Serialised size of one entry, near enough for batching decisions. */
export function approximateBytes(candidate: LogEntry): number {
  let total = Buffer.byteLength(candidate.line, 'utf8') + 24;
  for (const [key, value] of Object.entries(candidate.structuredMetadata ?? {})) {
    total += key.length + value.length + 8;
  }
  return total;
}

/**
 * Reject a label set Loki will refuse, or that will cost a fortune.
 *
 * Throws PermanentError, because every failure here is a code or config bug that
 * no amount of retrying changes.
 */
export function validateLabels(labels: Labels): void {
  const names = Object.keys(labels);
  if (names.length === 0) throw new PermanentError('a Loki stream needs at least one label');
  if (names.length > MAX_LABELS_PER_STREAM) {
    throw new PermanentError(
      `${names.length} labels exceeds the ${MAX_LABELS_PER_STREAM} label ceiling: ` +
        `${names.sort().join(', ')}. Move high-cardinality values to structured metadata.`,
    );
  }
  for (const name of names) {
    if (!LABEL_NAME_RE.test(name)) {
      throw new PermanentError(
        `label name "${name}" is not valid Loki syntax (need [a-zA-Z_][a-zA-Z0-9_]*)`,
      );
    }
    if (BANNED_LABEL_NAMES.has(name.toLowerCase())) {
      throw new PermanentError(
        `label "${name}" is high-cardinality and banned. Pass it as structured metadata ` +
          `on the entry instead. See docs/loki-ingestion.md.`,
      );
    }
    if (labels[name] === '') throw new PermanentError(`label "${name}" has an empty value`);
  }
}

function streamKey(labels: Labels): string {
  return JSON.stringify(Object.entries(labels).sort(([a], [b]) => (a < b ? -1 : 1)));
}

/** Collapse [labels, entry] pairs into the stream batch push() wants. */
export function groupByLabels(pairs: Iterable<readonly [Labels, LogEntry]>): Stream[] {
  const grouped = new Map<string, { labels: Labels; entries: LogEntry[] }>();
  for (const [labels, item] of pairs) {
    const key = streamKey(labels);
    const bucket = grouped.get(key) ?? { labels, entries: [] };
    bucket.entries.push(item);
    grouped.set(key, bucket);
  }
  return [...grouped.values()].map(({ labels, entries }) => [labels, entries] as Stream);
}

export type TokenProvider = () => string | Promise<string>;

/**
 * The request this client actually makes. Narrower than RequestInit on purpose:
 * it names exactly what is sent, so a test double is a few lines rather than a
 * fetch mock, and the body type does not depend on DOM lib being present.
 */
export interface LokiRequestInit {
  readonly method: 'POST';
  readonly headers: Record<string, string>;
  readonly body: Uint8Array;
  readonly signal: AbortSignal;
}

/** Injectable so a test exercises the real request building and retry paths. */
export type FetchLike = (url: string, init: LokiRequestInit) => Promise<Response>;

export class LokiClient {
  private readonly config: LokiConfig;
  private readonly tokenProvider: TokenProvider;
  private readonly fetchImpl: FetchLike;
  private authorization: string | undefined;

  constructor(config: LokiConfig, tokenProvider: TokenProvider, fetchImpl?: FetchLike) {
    this.config = config;
    this.tokenProvider = tokenProvider;
    // LokiRequestInit is structurally assignable to RequestInit, so no cast is
    // needed; the narrow type exists to document exactly what is sent and to
    // keep a test double small, not to work around the platform type.
    this.fetchImpl = fetchImpl ?? ((url, init) => fetch(url, init));
  }

  private async authHeader(refresh: boolean): Promise<string> {
    if (this.authorization === undefined || refresh) {
      const token = await this.tokenProvider();
      this.authorization = `Basic ${Buffer.from(`${this.config.tenantId}:${token}`).toString('base64')}`;
    }
    return this.authorization;
  }

  /**
   * Serialise streams into a Loki push body.
   *
   * Entries are sorted ascending per stream. Loki accepts some out-of-order
   * ingestion, but an unsorted batch can be partially rejected with a 400 that
   * names only the first offending line, which is miserable to debug.
   */
  buildPayload(streams: readonly Stream[]): Buffer {
    const merged = new Map<string, { labels: Labels; entries: LogEntry[] }>();
    for (const [labels, entries] of streams) {
      const resolved = { ...this.config.staticLabels, ...labels };
      validateLabels(resolved);
      const key = streamKey(resolved);
      const bucket = merged.get(key) ?? { labels: resolved, entries: [] };
      bucket.entries.push(...entries);
      merged.set(key, bucket);
    }

    const body = {
      streams: [...merged.values()]
        .filter(({ entries }) => entries.length > 0)
        .map(({ labels, entries }) => ({
          stream: labels,
          values: [...entries]
            .sort((a, b) => (a.timestampNs < b.timestampNs ? -1 : 1))
            .map((item) =>
              item.structuredMetadata && Object.keys(item.structuredMetadata).length > 0
                ? [item.timestampNs.toString(), item.line, item.structuredMetadata]
                : [item.timestampNs.toString(), item.line],
            ),
        })),
    };
    return Buffer.from(JSON.stringify(body), 'utf8');
  }

  /**
   * Push one batch. Resolves to the number of lines accepted.
   *
   * Throws PermanentError for anything a retry cannot fix, and RetryableError
   * only after the retry budget is spent - so the caller's error handling sees a
   * spent budget, not a transient blip.
   */
  async push(streams: readonly Stream[]): Promise<number> {
    const lineCount = streams.reduce((total, [, entries]) => total + entries.length, 0);
    if (lineCount === 0) return 0;

    const payload = this.buildPayload(streams);
    const body = this.config.compress ? gzipSync(payload, { level: 6 }) : payload;

    let lastError: Error | undefined;
    for (let attempt = 1; attempt <= this.config.maxRetries; attempt += 1) {
      const headers: Record<string, string> = {
        'content-type': 'application/json',
        'user-agent': 'grafana-cloud-reference-examples/loki-nodejs',
        authorization: await this.authHeader(attempt > 1),
      };
      if (this.config.compress) headers['content-encoding'] = 'gzip';

      // AbortSignal.timeout rather than a manual timer: fetch has no timeout
      // option, and a Lambda that hangs on a socket burns its whole duration.
      const signal = AbortSignal.timeout(this.config.requestTimeoutSeconds * 1000);

      try {
        const response = await this.fetchImpl(this.config.pushUrl, {
          method: 'POST',
          headers,
          body: body,
          signal,
        });

        if (response.ok) {
          LOG.debug('loki push accepted', {
            status: response.status,
            lines: lineCount,
            streams: streams.length,
            payload_bytes: body.byteLength,
            attempt,
          });
          return lineCount;
        }
        lastError = await this.classify(response, attempt);
      } catch (cause) {
        // An aborted fetch and a DNS failure both land here, and both are worth
        // retrying.
        lastError = new RetryableError(`Loki transport error: ${String(cause)}`);
        LOG.warning('loki push transport failure', { attempt, cause: String(cause) });
      }

      if (lastError instanceof PermanentError) throw lastError;
      if (attempt < this.config.maxRetries) {
        await sleep(this.backoffSeconds(attempt, lastError) * 1000);
      }
    }

    throw new RetryableError(
      `Loki push failed after ${this.config.maxRetries} attempts: ${lastError?.message ?? 'unknown'}`,
    );
  }

  private async classify(response: Response, attempt: number): Promise<Error> {
    const detail = (await response.text().catch(() => '<body unavailable>')).slice(0, 512);
    const status = response.status;

    if (RETRYABLE_STATUSES.has(status)) {
      const retryAfter = parseRetryAfter(response.headers.get('retry-after'));
      LOG.warning('loki push rejected, will retry', {
        status,
        attempt,
        retry_after_seconds: retryAfter,
        detail,
      });
      return new RetryableError(`Loki ${status}: ${detail}`, retryAfter);
    }

    if (status === 401 || status === 403) {
      // Retry once with a freshly fetched token, in case of rotation, then give
      // up: a wrong scope on the access policy never recovers.
      if (attempt === 1) {
        LOG.warning('loki push unauthorised, refreshing credential', { status });
        this.authorization = undefined;
        return new RetryableError(`Loki ${status}: ${detail}`);
      }
      return new PermanentError(
        `Loki ${status}: ${detail}. Check the tenant id and that the Cloud Access ` +
          `Policy token carries the logs:write scope.`,
        status,
      );
    }

    return new PermanentError(`Loki ${status}: ${detail}`, status);
  }

  /**
   * Exponential backoff with full jitter, capped, honouring Retry-After.
   *
   * Full jitter rather than a fixed multiplier because a fan-out of Lambdas
   * triggered by one S3 prefix retries in lockstep otherwise, and hits the same
   * rate limit again at the same instant.
   */
  private backoffSeconds(attempt: number, error: Error | undefined): number {
    if (error instanceof RetryableError && error.retryAfterSeconds !== undefined) {
      return Math.min(error.retryAfterSeconds, 30);
    }
    const ceiling = Math.min(2 ** (attempt - 1), 30);
    return 0.1 + Math.random() * (ceiling - 0.1);
  }
}

/**
 * Parse the delta-seconds form of Retry-After. The HTTP-date form is ignored:
 * parsing it needs a clock-skew assumption worse than local backoff, and Loki
 * sends delta-seconds.
 */
function parseRetryAfter(value: string | null): number | undefined {
  if (value === null || value.trim() === '') return undefined;
  const seconds = Number(value.trim());
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : undefined;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
