/**
 * Accumulate log entries and flush them as bounded batches.
 *
 * A Lambda reading a 500 MB gzipped object cannot hold every line in memory, and
 * pushing one line per request is both slow and rate-limited. This sits between.
 *
 * Bounded by line count and by approximate byte size, and both matter: 5,000
 * tiny lines is a fine batch, 5,000 lines of a 200 KB JSON blob each is not.
 */

import {
  approximateBytes,
  groupByLabels,
  type Labels,
  type LogEntry,
  type Stream,
} from './loki.js';

export class StreamBatcher {
  private readonly maxLines: number;
  private readonly maxBytes: number;
  private pending: Array<readonly [Labels, LogEntry]> = [];
  private pendingBytes = 0;

  constructor(options: { maxLines: number; maxBytes: number }) {
    if (options.maxLines <= 0 || options.maxBytes <= 0) {
      throw new RangeError('maxLines and maxBytes must both be positive');
    }
    this.maxLines = options.maxLines;
    this.maxBytes = options.maxBytes;
  }

  get size(): number {
    return this.pending.length;
  }

  get bytes(): number {
    return this.pendingBytes;
  }

  /**
   * Add one entry. Returns a batch when a bound is reached, else undefined.
   *
   * A single entry larger than maxBytes is still emitted on its own rather than
   * rejected: dropping a customer's log line because it is big is worse than one
   * oversized request, and Loki says so clearly if it refuses.
   */
  add(labels: Labels, item: LogEntry): Stream[] | undefined {
    this.pending.push([labels, item]);
    this.pendingBytes += approximateBytes(item);
    if (this.pending.length >= this.maxLines || this.pendingBytes >= this.maxBytes) {
      return this.flush();
    }
    return undefined;
  }

  /** Emit whatever is buffered, or undefined when empty. */
  flush(): Stream[] | undefined {
    if (this.pending.length === 0) return undefined;
    const batch = groupByLabels(this.pending);
    this.pending = [];
    this.pendingBytes = 0;
    return batch;
  }

  /**
   * Consume an async iterable of pairs and yield every batch, including the last.
   *
   * The common shape in a handler:
   *
   *     for await (const batch of batcher.drain(reader.entries(ref))) {
   *       await client.push(batch);
   *     }
   */
  async *drain(
    pairs: AsyncIterable<readonly [Labels, LogEntry]>,
  ): AsyncGenerator<Stream[], void, undefined> {
    for await (const [labels, item] of pairs) {
      const batch = this.add(labels, item);
      if (batch !== undefined) yield batch;
    }
    const final = this.flush();
    if (final !== undefined) yield final;
  }
}
