/**
 * AWS-specific helpers.
 *
 * Everything in this entry point may import the AWS SDK v3. Everything in the
 * package root must not - that split is what keeps a minimal example's
 * deployment package small, and a test asserts it.
 */

export {
  CredentialProvider,
  parseSecret,
  DEFAULT_CACHE_TTL_SECONDS,
  type CredentialProviderOptions,
  type GrafanaCloudCredential,
  type SecretsSender,
} from './credentials.js';

export {
  parseEvent,
  processMessages,
  toResponse,
  type BatchOutcome,
  type LambdaContext,
  type ProcessOptions,
  type SourceMessage,
} from './handler.js';

export {
  detectFormat,
  isCompressed,
  objectUri,
  S3ObjectReader,
  type RecordFormat,
  type RecordLine,
  type S3ObjectReaderOptions,
  type S3ObjectRef,
  type S3Sender,
} from './s3.js';
