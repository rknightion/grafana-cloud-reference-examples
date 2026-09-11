/**
 * Structured JSON logging for the function's own telemetry.
 *
 * This is what the *function* emits about itself, into CloudWatch. It is not the
 * data being shipped to Loki - keep the two straight, because a debug line about
 * a batch is not a customer log line.
 *
 * JSON rather than text so a Logs Insights query or a Grafana Cloud CloudWatch
 * integration can filter on fields without regex.
 */

export type LogFields = Record<string, unknown>;

const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40 } as const;
export type LogLevel = keyof typeof LEVELS;

/**
 * Any field whose key looks like one of these is replaced before serialisation.
 * A log line carrying a Cloud Access Policy token is a credential leak into
 * CloudWatch, readable by anyone with logs:FilterLogEvents.
 */
const SENSITIVE_KEY_FRAGMENTS = [
  'token',
  'secret',
  'password',
  'passwd',
  'authorization',
  'credential',
  'apikey',
  'api_key',
];

const REDACTED = '***redacted***';

function isSensitive(key: string): boolean {
  const lowered = key.toLowerCase();
  return SENSITIVE_KEY_FRAGMENTS.some((fragment) => lowered.includes(fragment));
}

export function redact(fields: LogFields): LogFields {
  return Object.fromEntries(
    Object.entries(fields).map(([key, value]) => [key, isSensitive(key) ? REDACTED : value]),
  );
}

function resolveLevel(raw: string | undefined): LogLevel {
  const candidate = (raw ?? 'INFO').toUpperCase();
  return candidate in LEVELS ? (candidate as LogLevel) : 'INFO';
}

export class Logger {
  private readonly name: string;
  private readonly bound: LogFields;
  private readonly level: LogLevel;
  private readonly threshold: number;

  constructor(name: string, bound: LogFields = {}, level?: LogLevel) {
    this.name = name;
    this.bound = bound;
    this.level = level ?? resolveLevel(process.env['LOG_LEVEL']);
    this.threshold = LEVELS[this.level];
  }

  /**
   * Return a logger that adds these fields to every subsequent line.
   *
   * Carries the resolved level forward. Without that, binding a field on a
   * logger built with an explicit level silently reverts it to whatever
   * LOG_LEVEL says.
   */
  bind(fields: LogFields): Logger {
    return new Logger(this.name, { ...this.bound, ...fields }, this.level);
  }

  private emit(level: LogLevel, message: string, fields: LogFields): void {
    if (LEVELS[level] < this.threshold) return;
    const payload = {
      level,
      logger: this.name,
      message,
      timestamp_ms: Date.now(),
      ...redact({ ...this.bound, ...fields }),
    };
    // The one place process.stdout is used directly: console.log in the Lambda
    // Node runtime adds its own formatting, which breaks the one-line-one-JSON
    // -object contract this relies on.
    process.stdout.write(`${JSON.stringify(payload)}\n`);
  }

  debug(message: string, fields: LogFields = {}): void {
    this.emit('DEBUG', message, fields);
  }

  info(message: string, fields: LogFields = {}): void {
    this.emit('INFO', message, fields);
  }

  warning(message: string, fields: LogFields = {}): void {
    this.emit('WARNING', message, fields);
  }

  error(message: string, fields: LogFields = {}): void {
    this.emit('ERROR', message, fields);
  }
}

export function getLogger(name: string, fields: LogFields = {}): Logger {
  return new Logger(name, fields);
}
