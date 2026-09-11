/**
 * Shared building blocks for the Node.js reference examples.
 *
 * Import from here, not from the submodules, so an example's imports stay stable
 * when internals move.
 *
 * This entry point has no runtime dependencies: global `fetch` and `node:zlib`
 * only. AWS helpers live behind the `@grafana-cloud/common/aws` entry point and
 * are imported separately, so a non-AWS example never pulls the SDK in.
 */

export {
  configFromEnv,
  normalisePushUrl,
  DEFAULT_BATCH_MAX_BYTES,
  DEFAULT_BATCH_MAX_LINES,
  DEFAULT_MAX_RETRIES,
  DEFAULT_REQUEST_TIMEOUT_SECONDS,
  type LokiConfig,
} from './config.js';

export {
  ConfigError,
  GrafanaCloudError,
  ParseError,
  PermanentError,
  RetryableError,
} from './errors.js';

export { getLogger, redact, Logger, type LogFields, type LogLevel } from './logger.js';

export {
  approximateBytes,
  entry,
  groupByLabels,
  nowNs,
  validateLabels,
  BANNED_LABEL_NAMES,
  LokiClient,
  MAX_LABELS_PER_STREAM,
  type FetchLike,
  type Labels,
  type LogEntry,
  type Stream,
  type TokenProvider,
} from './loki.js';

export { StreamBatcher } from './batching.js';
