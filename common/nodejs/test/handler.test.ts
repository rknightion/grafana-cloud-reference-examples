import { describe, expect, it } from 'vitest';

import { PermanentError } from '../src/errors.js';
import { parseEvent, processMessages, toResponse } from '../src/aws/handler.js';
import type { S3ObjectRef } from '../src/aws/s3.js';

function s3Record(key: string, bucket = 'acme-logs', size = 42): Record<string, unknown> {
  return {
    eventSource: 'aws:s3',
    eventName: 'ObjectCreated:Put',
    s3: { bucket: { name: bucket }, object: { key, size } },
  };
}

function removalRecord(key: string): Record<string, unknown> {
  return { ...s3Record(key), eventName: 'ObjectRemoved:Delete' };
}

function sqsRecord(messageId: string, body: unknown): Record<string, unknown> {
  return {
    eventSource: 'aws:sqs',
    messageId,
    body: typeof body === 'string' ? body : JSON.stringify(body),
  };
}

describe('parseEvent', () => {
  it('handles a direct S3 notification', () => {
    const messages = parseEvent({ Records: [s3Record('logs/app.log')] });
    expect(messages).toHaveLength(1);
    expect(messages[0]?.messageId).toBeUndefined();
    expect(messages[0]?.objects[0]).toEqual({
      bucket: 'acme-logs',
      key: 'logs/app.log',
      sizeBytes: 42,
    });
  });

  it('URL-decodes the object key', () => {
    // S3 encodes the key and uses '+' for spaces. Skipping this produces a
    // NoSuchKey that reads like a permissions problem.
    const messages = parseEvent({ Records: [s3Record('logs/my+file+%231.log.gz')] });
    expect(messages[0]?.objects[0]?.key).toBe('logs/my file #1.log.gz');
  });

  it('keeps the message id for an SQS-wrapped notification', () => {
    const messages = parseEvent({
      Records: [sqsRecord('m-1', { Records: [s3Record('a.log')] })],
    });
    expect(messages[0]?.messageId).toBe('m-1');
    expect(messages[0]?.objects[0]?.key).toBe('a.log');
  });

  it('ignores the S3 test event', () => {
    // S3 sends this once when a notification config is created. A handler that
    // assumes Records exists crashes on the first event after deploy.
    const body = { Service: 'Amazon S3', Event: 's3:TestEvent', Bucket: 'acme-logs' };
    expect(parseEvent({ Records: [sqsRecord('m-1', body)] })).toEqual([]);
  });

  it('reports a poison SQS body as failed rather than dropping it', () => {
    const messages = parseEvent({ Records: [sqsRecord('m-1', 'this is not json')] });
    expect(messages).toEqual([{ objects: [], messageId: 'm-1' }]);
  });

  it('handles an EventBridge Object Created event without decoding the key', () => {
    const messages = parseEvent({
      'detail-type': 'Object Created',
      detail: { bucket: { name: 'acme-logs' }, object: { key: 'logs/my file.log', size: 9 } },
    });
    expect(messages[0]?.objects[0]?.key).toBe('logs/my file.log');
  });

  it('yields no work for a removal notification', () => {
    // The object is already gone, so acting on it can only produce a NoSuchKey
    // and a misleading log line.
    expect(parseEvent({ Records: [removalRecord('gone.log')] })).toEqual([]);
  });

  it('acknowledges an SQS-wrapped removal rather than failing it', () => {
    // Nothing to do is a success. Failing it would send every delete in the
    // bucket to the dead-letter queue.
    const event = { Records: [sqsRecord('m-1', { Records: [removalRecord('gone.log')] })] };
    expect(parseEvent(event)).toEqual([]);
  });

  it('ignores an EventBridge delete rather than calling it an unknown shape', () => {
    const event = {
      'detail-type': 'Object Deleted',
      detail: { bucket: { name: 'acme-logs' }, object: { key: 'gone.log' } },
    };
    expect(parseEvent(event)).toEqual([]);
  });

  it('tolerates a missing size on a create event', () => {
    const record = s3Record('a.log');
    delete (record['s3'] as { object: Record<string, unknown> }).object['size'];
    const messages = parseEvent({ Records: [record] });
    expect(messages[0]?.objects[0]?.sizeBytes).toBeUndefined();
  });

  it('reports a non-string SQS body as failed rather than dropping it', () => {
    // A skip here would have Lambda delete the message as handled, so a
    // malformed record would vanish without reaching the DLQ.
    const messages = parseEvent({ Records: [{ eventSource: 'aws:sqs', messageId: 'm-1' }] });
    expect(messages).toEqual([{ objects: [], messageId: 'm-1' }]);
  });

  it('reports an SQS body that is JSON but not an object as failed', () => {
    const messages = parseEvent({ Records: [sqsRecord('m-1', [1, 2, 3])] });
    expect(messages).toEqual([{ objects: [], messageId: 'm-1' }]);
  });

  it('throws a permanent error on an unrecognised event', () => {
    expect(() => parseEvent({ someOtherTrigger: true })).toThrow(PermanentError);
  });
});

describe('processMessages', () => {
  const ref = (key: string): S3ObjectRef => ({ bucket: 'b', key });

  it('counts successes', async () => {
    const outcome = await processMessages([{ objects: [ref('k')], messageId: 'm-1' }], () =>
      Promise.resolve(7),
    );
    expect(outcome.linesShipped).toBe(7);
    expect(outcome.objectsProcessed).toBe(1);
    expect(outcome.failedMessageIds).toEqual([]);
  });

  it('fails only the message that owns the bad object', async () => {
    const outcome = await processMessages(
      [
        { objects: [ref('good')], messageId: 'm-1' },
        { objects: [ref('bad')], messageId: 'm-2' },
        { objects: [ref('also-good')], messageId: 'm-3' },
      ],
      (candidate) =>
        candidate.key === 'bad' ? Promise.reject(new PermanentError('nope')) : Promise.resolve(1),
    );
    expect(outcome.failedMessageIds).toEqual(['m-2']);
    expect(outcome.objectsProcessed).toBe(2);
    expect(toResponse(outcome)).toEqual({ batchItemFailures: [{ itemIdentifier: 'm-2' }] });
  });

  it('stops early and fails the remainder when out of time', async () => {
    // A timeout kill returns no partial-failure response at all, so the whole
    // batch redelivers and every shipped line is duplicated.
    const context = {
      awsRequestId: 'r',
      functionName: 'f',
      getRemainingTimeInMillis: () => 1_000,
    };
    const outcome = await processMessages(
      [0, 1, 2].map((index) => ({ objects: [ref(`k${index}`)], messageId: `m-${index}` })),
      () => Promise.resolve(1),
      { context, timeBudgetMs: 15_000 },
    );
    expect(outcome.objectsProcessed).toBe(0);
    expect(outcome.failedMessageIds).toEqual(['m-0', 'm-1', 'm-2']);
  });

  it('fails a message that has an id and no objects', async () => {
    // This is how parseEvent reports a malformed SQS record: nothing to
    // process, and it must not be acknowledged.
    const outcome = await processMessages([{ objects: [], messageId: 'm-1' }], () =>
      Promise.resolve(0),
    );
    expect(outcome.failedMessageIds).toEqual(['m-1']);
    expect(outcome.objectsProcessed).toBe(0);
  });

  it('treats a message with no id and no objects as a no-op', async () => {
    const outcome = await processMessages([{ objects: [] }], () => Promise.resolve(0));
    expect(outcome.failedMessageIds).toEqual([]);
  });

  it('proceeds when there is time', async () => {
    const context = {
      awsRequestId: 'r',
      functionName: 'f',
      getRemainingTimeInMillis: () => 300_000,
    };
    const outcome = await processMessages(
      [{ objects: [ref('k')], messageId: 'm-1' }],
      () => Promise.resolve(3),
      { context },
    );
    expect(outcome.linesShipped).toBe(3);
    expect(outcome.failedMessageIds).toEqual([]);
  });
});
