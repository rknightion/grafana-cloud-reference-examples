/**
 * Stream an S3 object into log lines without buffering the whole thing.
 *
 * The object is read as a stream and decompressed on the fly, so a 2 GB gzipped
 * export runs in a 256 MB function. Nothing here reads the body in one go except
 * the JSON-array path, which is explicitly bounded.
 */

import { createGunzip } from 'node:zlib';
import { createInterface } from 'node:readline';
import { Readable } from 'node:stream';

import {
  GetObjectCommand,
  S3Client,
  type GetObjectCommandOutput,
  type S3ClientConfig,
} from '@aws-sdk/client-s3';

import { ParseError, PermanentError, RetryableError } from '../errors.js';
import { getLogger } from '../logger.js';

const LOG = getLogger('aws.s3');

const GZIP_SUFFIXES = ['.gz', '.gzip'];

export type RecordFormat = 'lines' | 'jsonl' | 'json_array' | 'csv';

export interface S3ObjectRef {
  readonly bucket: string;
  readonly key: string;
  readonly sizeBytes?: number;
  readonly versionId?: string;
}

export function objectUri(ref: S3ObjectRef): string {
  return `s3://${ref.bucket}/${ref.key}`;
}

export function isCompressed(key: string): boolean {
  const lowered = key.toLowerCase();
  return GZIP_SUFFIXES.some((suffix) => lowered.endsWith(suffix));
}

/**
 * Guess the record format from the key, ignoring any compression suffix.
 *
 * A guess, not a contract. An example whose input format is known should set it
 * explicitly rather than relying on this.
 */
export function detectFormat(key: string): RecordFormat {
  let lowered = key.toLowerCase();
  for (const suffix of GZIP_SUFFIXES) {
    if (lowered.endsWith(suffix)) {
      lowered = lowered.slice(0, -suffix.length);
      break;
    }
  }
  if (lowered.endsWith('.jsonl') || lowered.endsWith('.ndjson')) return 'jsonl';
  if (lowered.endsWith('.json')) return 'json_array';
  if (lowered.endsWith('.csv') || lowered.endsWith('.tsv')) return 'csv';
  return 'lines';
}

/**
 * Just the one call this makes. Narrower than the SDK client so a test double is
 * a few lines, and so the overloaded `send` signature does not leak out.
 */
export interface S3Sender {
  send(command: GetObjectCommand): Promise<GetObjectCommandOutput>;
}

export interface S3ObjectReaderOptions {
  readonly client?: S3Sender;
  readonly clientConfig?: S3ClientConfig;
  readonly maxJsonBytes?: number;
}

export interface RecordLine {
  readonly recordNumber: number;
  readonly line: string;
}

/**
 * Reads S3 objects as an async iterable of log lines.
 *
 * One instance per execution environment: the SDK client holds a connection
 * pool, and rebuilding it per invocation adds a TLS handshake to every call.
 */
export class S3ObjectReader {
  private client: S3Sender | undefined;
  private readonly clientConfig: S3ClientConfig;
  private readonly maxJsonBytes: number;

  constructor(options: S3ObjectReaderOptions = {}) {
    this.client = options.client;
    this.clientConfig = options.clientConfig ?? {};
    this.maxJsonBytes = options.maxJsonBytes ?? 64 * 1024 * 1024;
  }

  private s3(): S3Sender {
    this.client ??= new S3Client(this.clientConfig);
    return this.client;
  }

  /**
   * Yield `{ recordNumber, line }`, 1-indexed.
   *
   * The record number is the caller's only handle on where a line came from, so
   * it goes into structured metadata rather than being discarded. Blank lines are
   * skipped; Loki has no use for them and they still cost ingest.
   */
  async *lines(ref: S3ObjectRef, recordFormat?: RecordFormat): AsyncGenerator<RecordLine> {
    const resolved = recordFormat ?? detectFormat(ref.key);

    let response: GetObjectCommandOutput;
    try {
      response = await this.s3().send(
        new GetObjectCommand({
          Bucket: ref.bucket,
          Key: ref.key,
          ...(ref.versionId ? { VersionId: ref.versionId } : {}),
        }),
      );
    } catch (cause) {
      throw classifyS3Error(cause, ref);
    }

    if (response.Body === undefined) {
      throw new ParseError(`${objectUri(ref)}: response had no body`);
    }

    // The SDK v3 Body is a Node Readable on Lambda. Piping through gunzip keeps
    // the read streaming rather than materialising the object.
    const raw = response.Body as Readable;
    const stream = isCompressed(ref.key) ? raw.pipe(createGunzip()) : raw;

    LOG.debug('reading s3 object', { uri: objectUri(ref), record_format: resolved });

    try {
      if (resolved === 'json_array') {
        yield* this.jsonArray(stream, ref);
        return;
      }
      yield* this.lineOriented(stream, resolved, ref);
    } catch (cause) {
      if (cause instanceof ParseError) throw cause;
      // zlib rejecting the stream means the object is not gzip despite its key,
      // which no retry can change. Everything else here is a read failure - a
      // socket reset, a read timeout, a stream that ended early - and treating
      // one of those as permanent means the object is never processed at all. A
      // genuinely truncated object still lands in the DLQ once the redelivery
      // count is spent.
      const code = (cause as { code?: string }).code ?? '';
      if (code.startsWith('Z_')) {
        throw new ParseError(`${objectUri(ref)}: not a gzip stream (${code})`);
      }
      throw new RetryableError(`${objectUri(ref)}: read failed: ${String(cause)}`);
    } finally {
      raw.destroy();
    }
  }

  private async *lineOriented(
    stream: Readable,
    recordFormat: Exclude<RecordFormat, 'json_array'>,
    ref: S3ObjectRef,
  ): AsyncGenerator<RecordLine> {
    const reader = createInterface({ input: stream, crlfDelay: Infinity });
    let recordNumber = 0;
    let lineNumber = 0;
    let csvHeader: string[] | undefined;
    const delimiter = csvDelimiter(ref.key);

    for await (const raw of reader) {
      lineNumber += 1;
      const line = raw.trim();
      if (line === '') continue;

      if (recordFormat === 'lines') {
        recordNumber += 1;
        yield { recordNumber, line: raw.replace(/\r$/, '') };
        continue;
      }

      if (recordFormat === 'jsonl') {
        recordNumber += 1;
        let parsed: unknown;
        try {
          parsed = JSON.parse(line);
        } catch (cause) {
          throw new ParseError(
            `${objectUri(ref)}: line ${lineNumber} is not valid JSON: ${String(cause)}`,
          );
        }
        yield { recordNumber, line: JSON.stringify(parsed) };
        continue;
      }

      // CSV. The first non-blank line is the header.
      const cells = splitDelimited(line, delimiter);
      if (csvHeader === undefined) {
        csvHeader = cells;
        continue;
      }
      recordNumber += 1;
      const row: Record<string, string> = {};
      cells.forEach((cell, index) => {
        row[csvHeader?.[index] ?? '_extra'] = cell;
      });
      yield { recordNumber, line: JSON.stringify(row) };
    }

    if (recordFormat === 'csv' && csvHeader === undefined) {
      throw new ParseError(`${objectUri(ref)}: CSV has no header row`);
    }
  }

  private async *jsonArray(stream: Readable, ref: S3ObjectRef): AsyncGenerator<RecordLine> {
    // Read whole, bounded. Streaming a JSON array needs a pull parser and a
    // dependency; the bound is the honest alternative.
    const chunks: Buffer[] = [];
    let total = 0;
    for await (const chunk of stream) {
      const buffer = chunk as Buffer;
      total += buffer.byteLength;
      if (total > this.maxJsonBytes) {
        throw new ParseError(
          `${objectUri(ref)}: JSON document exceeds maxJsonBytes (${this.maxJsonBytes}). ` +
            `Re-export as JSON Lines.`,
        );
      }
      chunks.push(buffer);
    }

    let document: unknown;
    try {
      document = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch (cause) {
      throw new ParseError(`${objectUri(ref)}: not valid JSON: ${String(cause)}`);
    }
    const elements = Array.isArray(document) ? document : [document];
    for (const [index, element] of elements.entries()) {
      yield { recordNumber: index + 1, line: JSON.stringify(element) };
    }
  }
}

function csvDelimiter(key: string): string {
  let base = key.toLowerCase();
  for (const suffix of GZIP_SUFFIXES) {
    if (base.endsWith(suffix)) base = base.slice(0, -suffix.length);
  }
  return base.endsWith('.tsv') ? '\t' : ',';
}

/**
 * Minimal RFC 4180 split: handles quoted cells containing the delimiter and
 * escaped double quotes. Deliberately not a full CSV library - a dependency for
 * this would be the only one in the package.
 */
function splitDelimited(line: string, delimiter: string): string[] {
  const cells: string[] = [];
  let current = '';
  let inQuotes = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (inQuotes) {
      if (char === '"' && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        current += char;
      }
    } else if (char === '"') {
      inQuotes = true;
    } else if (char === delimiter) {
      cells.push(current);
      current = '';
    } else {
      current += char;
    }
  }
  cells.push(current);
  return cells;
}

/**
 * Map an SDK error to the retryable/permanent split.
 *
 * NoSuchKey is permanent and common: an object deleted between the S3 event and
 * the invocation. Retrying it burns the whole redrive budget for nothing.
 */
function classifyS3Error(cause: unknown, ref: S3ObjectRef): Error {
  const named = cause as { name?: string; $metadata?: { httpStatusCode?: number } };
  const code = named.name ?? '';
  const status = named.$metadata?.httpStatusCode ?? 0;

  if (code === 'NoSuchKey' || code === 'NoSuchVersion' || status === 404) {
    return new PermanentError(
      `${objectUri(ref)}: object does not exist (deleted after the event?)`,
    );
  }
  if (code === 'AccessDenied' || status === 403) {
    return new PermanentError(
      `${objectUri(ref)}: access denied. The function role needs s3:GetObject on this ` +
        `prefix, and kms:Decrypt if the bucket uses SSE-KMS.`,
    );
  }
  if (['SlowDown', 'RequestTimeout', 'ServiceUnavailable', 'InternalError'].includes(code)) {
    return new RetryableError(`${objectUri(ref)}: S3 transient failure (${code})`);
  }
  if (status >= 500) {
    return new RetryableError(`${objectUri(ref)}: S3 transient failure (${status})`);
  }
  return new PermanentError(`${objectUri(ref)}: S3 error ${code || status}`);
}
