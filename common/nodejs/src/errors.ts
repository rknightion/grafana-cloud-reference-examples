/**
 * Error taxonomy.
 *
 * The split that matters in a Lambda is retryable vs permanent. A permanent
 * error thrown back to Lambda is retried by Lambda anyway, then lands in the
 * DLQ, then gets retried on redrive - so a bad label set or an unparseable
 * object can burn an entire concurrency budget on work that can never succeed.
 * Classify at the point of failure and let the handler decide.
 */

export class GrafanaCloudError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

/** Configuration is missing or invalid. Permanent by definition. */
export class ConfigError extends GrafanaCloudError {}

/** May succeed if tried again. Carries the server's own backoff hint where given. */
export class RetryableError extends GrafanaCloudError {
  readonly retryAfterSeconds: number | undefined;

  constructor(message: string, retryAfterSeconds?: number) {
    super(message);
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/** Will never succeed as submitted. Retrying wastes money and delays everything behind it. */
export class PermanentError extends GrafanaCloudError {
  readonly status: number | undefined;

  constructor(message: string, status?: number) {
    super(message);
    this.status = status;
  }
}

/** An object or record could not be parsed into log lines. */
export class ParseError extends PermanentError {}
