/**
 * Lambda event plumbing: parse the event, report partial failures.
 *
 * Three event shapes reach an S3-to-Loki function and all three are normal:
 *
 * - a direct S3 bucket notification
 * - an S3 notification delivered through SQS, which is what you want at any real
 *   volume because it gives batching, a retry policy and a DLQ
 * - an EventBridge `Object Created` event, if the bucket already routes to it
 *
 * They are normalised to the same S3ObjectRef list so a handler has one path.
 */

import { PermanentError } from '../errors.js';
import { getLogger } from '../logger.js';
import { objectUri, type S3ObjectRef } from './s3.js';

const LOG = getLogger('aws.handler');

/**
 * S3 sends this once when a notification configuration is created. It has no
 * Records key, and a handler assuming one crashes on the very first event after
 * deployment, which reads like a broken function.
 */
const S3_TEST_EVENT_SERVICE = 'Amazon S3';

/**
 * A removal notification names an object that is already gone, so acting on it
 * means a GetObject that can only ever return NoSuchKey. That is classified
 * permanent, so it does not loop, but it still costs an invocation and puts a
 * misleading error in the log. Filtered here instead.
 */
const IGNORED_EVENT_PREFIXES = ['ObjectRemoved'];

/** The subset of the Lambda context worth depending on. */
export interface LambdaContext {
  readonly awsRequestId: string;
  readonly functionName: string;
  getRemainingTimeInMillis(): number;
}

/**
 * One unit of work, and the SQS receipt that has to be reported on failure.
 *
 * `messageId` is undefined for a direct S3 or EventBridge invocation, where the
 * whole invocation succeeds or fails together.
 */
export interface SourceMessage {
  readonly objects: readonly S3ObjectRef[];
  readonly messageId?: string;
}

export interface BatchOutcome {
  readonly failedMessageIds: string[];
  objectsProcessed: number;
  objectsFailed: number;
  linesShipped: number;
}

/**
 * Build the SQS partial-batch-failure response.
 *
 * This only takes effect when the event source mapping has
 * `FunctionResponseTypes: [ReportBatchItemFailures]`. Without it Lambda ignores
 * the response and redelivers the whole batch, silently duplicating every line
 * already shipped. The shared Terraform module and the CloudFormation template
 * both set it.
 */
export function toResponse(outcome: BatchOutcome): {
  batchItemFailures: Array<{ itemIdentifier: string }>;
} {
  return {
    batchItemFailures: outcome.failedMessageIds.map((itemIdentifier) => ({ itemIdentifier })),
  };
}

function objectFromS3Record(record: Record<string, unknown>): S3ObjectRef | undefined {
  const eventName = typeof record['eventName'] === 'string' ? record['eventName'] : '';
  if (IGNORED_EVENT_PREFIXES.some((prefix) => eventName.startsWith(prefix))) return undefined;

  const s3Block = record['s3'] as Record<string, unknown> | undefined;
  if (typeof s3Block !== 'object' || s3Block === null) return undefined;
  const bucket = (s3Block['bucket'] as { name?: string } | undefined)?.name;
  const object = s3Block['object'] as
    { key?: string; size?: number; versionId?: string } | undefined;
  if (bucket === undefined || object?.key === undefined) return undefined;

  return {
    bucket,
    // S3 URL-encodes the key and encodes spaces as '+'. A handler that skips
    // this fails on any object whose name contains a space, a '#' or a '+', with
    // a NoSuchKey that looks like a permissions problem.
    key: decodeURIComponent(object.key.replace(/\+/g, ' ')),
    ...(typeof object.size === 'number' ? { sizeBytes: object.size } : {}),
    ...(object.versionId ? { versionId: object.versionId } : {}),
  };
}

function objectsFromEventBridge(detail: Record<string, unknown>): S3ObjectRef[] {
  const bucket = (detail['bucket'] as { name?: string } | undefined)?.name;
  const object = detail['object'] as
    { key?: string; size?: number; 'version-id'?: string } | undefined;
  if (bucket === undefined || object?.key === undefined) return [];
  return [
    {
      bucket,
      // EventBridge does NOT URL-encode the key, unlike an S3 notification.
      key: object.key,
      ...(typeof object.size === 'number' ? { sizeBytes: object.size } : {}),
      ...(object['version-id'] ? { versionId: object['version-id'] } : {}),
    },
  ];
}

/**
 * Normalise any supported event into units of work.
 *
 * Returns an empty array for an event carrying no objects to process - an S3
 * test event, or a removal notification. That is a success, not an error.
 *
 * A malformed SQS record is different: it comes back as a message with no
 * objects and its messageId set, so processMessages marks it failed and it
 * reaches the dead-letter queue instead of being silently acknowledged.
 */
export function parseEvent(event: Record<string, unknown>): SourceMessage[] {
  // Any EventBridge S3 event is recognised, so a delete is ignored rather than
  // reported as an unknown shape - but only a creation yields work. See
  // IGNORED_EVENT_PREFIXES.
  const detailType = event['detail-type'];
  if (typeof detailType === 'string' && detailType.startsWith('Object ')) {
    if (detailType !== 'Object Created') {
      LOG.debug('ignoring eventbridge event', { detail_type: detailType });
      return [];
    }
    const detail = event['detail'];
    const objects =
      typeof detail === 'object' && detail !== null
        ? objectsFromEventBridge(detail as Record<string, unknown>)
        : [];
    return objects.length > 0 ? [{ objects }] : [];
  }

  const records = event['Records'];
  if (!Array.isArray(records)) {
    throw new PermanentError(
      `unrecognised event shape; keys were ${Object.keys(event).sort().join(', ')}. ` +
        `Expected an S3 notification, an SQS batch, or an EventBridge S3 event.`,
    );
  }

  const messages: SourceMessage[] = [];
  const directObjects: S3ObjectRef[] = [];

  for (const raw of records) {
    if (typeof raw !== 'object' || raw === null) continue;
    const record = raw as Record<string, unknown>;

    if (record['eventSource'] === 'aws:sqs') {
      // Narrowed rather than String()'d: an SQS record with a non-string
      // messageId is malformed, and "[object Object]" as a batchItemFailures
      // identifier would silently fail to match anything.
      const rawMessageId = record['messageId'];
      const messageId = typeof rawMessageId === 'string' ? rawMessageId : '';
      const body = record['body'];
      if (typeof body !== 'string') {
        // Reported as failed rather than skipped. A `continue` here would have
        // Lambda delete the message as handled, so a malformed record would
        // vanish without ever reaching the DLQ.
        LOG.error('sqs record has no string body', { message_id: messageId });
        messages.push({ objects: [], messageId });
        continue;
      }
      let inner: unknown;
      try {
        inner = JSON.parse(body);
      } catch (cause) {
        // A poison message. Reported as failed so it reaches the DLQ rather
        // than being silently dropped.
        LOG.error('sqs body is not JSON', { message_id: messageId, cause: String(cause) });
        messages.push({ objects: [], messageId });
        continue;
      }
      if (typeof inner !== 'object' || inner === null || Array.isArray(inner)) {
        LOG.error('sqs body is not a JSON object', { message_id: messageId });
        messages.push({ objects: [], messageId });
        continue;
      }
      const payload = inner as Record<string, unknown>;
      if (payload['Service'] === S3_TEST_EVENT_SERVICE && 'Event' in payload) {
        LOG.info('ignoring s3 test event', { message_id: messageId });
        continue;
      }
      const innerRecords = payload['Records'];
      const objects = Array.isArray(innerRecords)
        ? innerRecords
            .filter(
              (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
            )
            .map(objectFromS3Record)
            .filter((item): item is S3ObjectRef => item !== undefined)
        : [];
      // A message that resolves to no objects - a removal notification, or a
      // shape carrying no s3 block - is a success with nothing to do, so it is
      // acknowledged rather than failed.
      if (objects.length > 0) messages.push({ objects, messageId });
      continue;
    }

    const ref = objectFromS3Record(record);
    if (ref !== undefined) directObjects.push(ref);
  }

  if (directObjects.length > 0) messages.push({ objects: directObjects });
  return messages;
}

export interface ProcessOptions {
  readonly context?: LambdaContext;
  /**
   * Stop early when less than this remains. A timeout kill returns no
   * partial-failure response at all, so the entire batch redelivers and every
   * line already shipped is duplicated.
   */
  readonly timeBudgetMs?: number;
}

/**
 * Run `handleObject` over every object, isolating failures per message.
 *
 * A failure on one object marks that message failed and every other message in
 * the batch still ships.
 */
export async function processMessages(
  messages: Iterable<SourceMessage>,
  handleObject: (ref: S3ObjectRef) => Promise<number>,
  options: ProcessOptions = {},
): Promise<BatchOutcome> {
  const timeBudgetMs = options.timeBudgetMs ?? 15_000;
  const outcome: BatchOutcome = {
    failedMessageIds: [],
    objectsProcessed: 0,
    objectsFailed: 0,
    linesShipped: 0,
  };
  let outOfTime = false;

  for (const message of messages) {
    if (outOfTime) {
      if (message.messageId) outcome.failedMessageIds.push(message.messageId);
      continue;
    }

    // parseEvent represents a malformed SQS record as "has an id, has no
    // objects". Nothing to process, and it must not be acknowledged.
    if (message.messageId && message.objects.length === 0) {
      outcome.failedMessageIds.push(message.messageId);
      continue;
    }

    const remaining = options.context?.getRemainingTimeInMillis();
    if (remaining !== undefined && remaining < timeBudgetMs) {
      LOG.warning('stopping early to preserve the partial-failure response', {
        remaining_ms: remaining,
        time_budget_ms: timeBudgetMs,
      });
      outOfTime = true;
      if (message.messageId) outcome.failedMessageIds.push(message.messageId);
      continue;
    }

    let failed = false;
    for (const ref of message.objects) {
      try {
        outcome.linesShipped += await handleObject(ref);
        outcome.objectsProcessed += 1;
      } catch (cause) {
        // Deliberately broad: one bad object must not fail the whole batch.
        failed = true;
        outcome.objectsFailed += 1;
        LOG.error('object failed', {
          uri: objectUri(ref),
          error_type: (cause as { name?: string }).name ?? typeof cause,
          cause: String(cause),
        });
      }
    }
    if (failed && message.messageId) outcome.failedMessageIds.push(message.messageId);
  }

  return outcome;
}
