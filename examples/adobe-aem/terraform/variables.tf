variable "name" {
  description = "Name for the function and its companion resources."
  type        = string
  default     = "grafana-cloud-adobe-aem"
}

variable "source_bucket_name" {
  description = <<-EOT
    The bucket Adobe AEM Cloud Service forwards logs into. Not created here;
    only s3:GetObject and a scoped s3:ListBucket are granted on it.
  EOT
  type        = string
}

variable "source_prefix" {
  description = "Only process keys under this prefix. Empty means the whole bucket."
  type        = string
  default     = ""
}

variable "source_suffix" {
  description = <<-EOT
    Only process keys with this suffix. Empty means any.

    Leave it empty unless the bucket holds more than AEM logs: Adobe forwards
    some log types gzipped and some plain, so a single suffix usually excludes
    half of them.
  EOT
  type        = string
  default     = ""
}

# --- AEM identity and key layout ---------------------------------------------

variable "key_pattern" {
  description = <<-EOT
    Regex with named groups for reading AEM coordinates off an object key.
    Recognised groups: program_id, env_id, env_type, tier, log_type.

    Empty uses the built-in default, which matches Adobe's own download naming -
    <tier>_<logtype>_<date>.log - and is searched rather than anchored, so any
    prefix in front of it is ignored.

    Set this to match your bucket. Adobe does not document a single S3 layout
    for forwarded logs, and a pattern that matches nothing sends every line to
    the log_type="unknown" stream. The function falls back to identifying the
    log type from the file's first line, so a wrong pattern degrades rather than
    breaking, but the labels will be less complete.
  EOT
  type        = string
  default     = ""
}

variable "aem_program_id" {
  description = <<-EOT
    Cloud Manager program id, e.g. p12345, as the aem_program_id label. Usually
    set here rather than parsed from the key. Empty omits the label entirely -
    which is better than a placeholder, because a label reading "unknown" still
    creates a stream and still looks like data on a dashboard.
  EOT
  type        = string
  default     = ""
}

variable "aem_env_id" {
  description = "Cloud Manager environment id, e.g. e67890, as the aem_env_id label."
  type        = string
  default     = ""
}

variable "aem_env_type" {
  description = "dev, stage or prod, as the aem_env_type label. Free-form; not validated."
  type        = string
  default     = ""
}

variable "aem_tier" {
  description = <<-EOT
    Fallback aem_tier label when the key pattern does not capture one:
    author, publish, preview or dispatcher.

    Only a fallback. A tier found in the object key always wins, so one function
    can serve a bucket holding every tier.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.aem_tier == "" || contains(["author", "publish", "preview", "dispatcher"], var.aem_tier)
    error_message = "aem_tier must be empty, or one of author, publish, preview, dispatcher."
  }
}

# --- Grafana Cloud -----------------------------------------------------------

variable "grafana_cloud_loki_endpoint" {
  description = <<-EOT
    Loki host, base URL, or full push URL. All three forms are accepted:
      logs-prod-012.grafana.net
      https://logs-prod-012.grafana.net
      https://logs-prod-012.grafana.net/loki/api/v1/push
  EOT
  type        = string

  validation {
    condition     = length(trimspace(var.grafana_cloud_loki_endpoint)) > 0
    error_message = "grafana_cloud_loki_endpoint must not be empty."
  }
}

variable "grafana_cloud_loki_tenant_id" {
  description = "Numeric Loki tenant (instance) id from the Grafana Cloud stack details page. Not your email."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.grafana_cloud_loki_tenant_id))
    error_message = "The Loki tenant id is numeric. A non-numeric value here is usually the username from a different integration."
  }
}

variable "credentials_secret_id" {
  description = <<-EOT
    Name or ARN of an existing Secrets Manager secret holding the Cloud Access
    Policy token. Create it before applying; the token is never a Terraform
    input, so it never lands in state.
  EOT
  type        = string
}

variable "credentials_kms_key_arn" {
  description = "KMS key ARN if the secret uses a customer-managed key. Null for the default key."
  type        = string
  default     = null
}

variable "static_labels" {
  description = <<-EOT
    Extra Loki stream labels applied to every line, e.g. { region = "eu-west-1" }.

    Low cardinality only, and read the label contract in the example README
    first. The seven labels this example sets are already chosen to bound stream
    count; anything added here multiplies it.
  EOT
  type        = map(string)
  default     = {}
}

# --- Parsing and labelling ---------------------------------------------------

variable "service_name" {
  description = "Value of the service_name label."
  type        = string
  default     = "adobe-aem"
}

variable "line_content" {
  description = <<-EOT
    raw ships the original log line; message ships only the human-readable part.

    raw is lossless and makes `|= "some string"` behave as expected. message
    drops the timestamp and node id that are already in structured metadata,
    saving roughly 40% of the ingest volume on an error log.
  EOT
  type        = string
  default     = "raw"

  validation {
    condition     = contains(["raw", "message"], var.line_content)
    error_message = "line_content must be raw or message."
  }
}

variable "sniff_content" {
  description = <<-EOT
    Identify the log type from the first line when the key pattern did not.
    Costs nothing - that line is read either way - and is what keeps a
    mismatched key_pattern from sending everything to the unknown stream.
    Set false to force strict key-pattern behaviour.
  EOT
  type        = bool
  default     = true
}

variable "correlate_requests" {
  description = <<-EOT
    Pair the AEM request log's `->` and `<-` lines so the response line carries
    the method and path it belongs to.

    On by default: without it the duration is unattributable and
    latency-by-path is impossible, because Loki has no join. Measured at 100% of
    responses paired within an object on real output.
  EOT
  type        = bool
  default     = true
}

variable "max_pending_requests" {
  description = <<-EOT
    Cap on buffered request-log lines awaiting their partner. Bounds memory on a
    very large object; exceeding it costs enrichment on the oldest lines, never
    the lines themselves.
  EOT
  type        = number
  default     = 20000

  validation {
    condition     = var.max_pending_requests >= 1 && var.max_pending_requests == floor(var.max_pending_requests)
    error_message = "max_pending_requests must be a positive whole number."
  }
}

variable "drop_client_ip" {
  description = <<-EOT
    Omit client_ip from structured metadata on the access and CDN logs.

    Kept by default because it drives the geography and abuse panels, and it is
    the customer's own traffic. Set true where a data-protection position says
    client addresses must not be stored.
  EOT
  type        = bool
  default     = false
}

variable "log_utc_offset_seconds" {
  description = <<-EOT
    Offset applied to the two timestamp formats that carry no timezone: the AEM
    Java error log and the Apache error log.

    AEM as a Cloud Service writes UTC, so 0 is correct there. A self-hosted AEM
    logging in local time needs this set, or every error line lands in the wrong
    place on the timeline.
  EOT
  type        = number
  default     = 0
}

variable "max_timestamp_age_seconds" {
  description = <<-EOT
    Stamp a line at ingestion time instead of its own event time once it is
    older than this. 0 disables the fallback and always uses the event time.

    Set it before replaying a historical export. Loki rejects a sample older
    than the tenant's reject_old_samples_max_age (one week on Grafana Cloud by
    default) with a 400 that no retry fixes, and one stale line fails the whole
    batch it travelled in.
  EOT
  type        = number
  default     = 0
}

variable "log_level" {
  description = "The function's own log level. DEBUG emits a line per batch and costs CloudWatch ingest."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.log_level)
    error_message = "log_level must be DEBUG, INFO, WARNING or ERROR."
  }
}

variable "extra_environment_variables" {
  description = "Additional environment variables merged over the ones this module sets."
  type        = map(string)
  default     = {}
}

# --- Packaging and sizing ----------------------------------------------------

variable "lambda_zip_path" {
  description = <<-EOT
    Path to the deployment zip. The default is where both the release bundle and
    `just package adobe-aem` put it, so no configuration is needed in either case.
  EOT
  type        = string
  default     = "lambda.zip"
}

variable "runtime" {
  description = <<-EOT
    Lambda runtime. Must match `runtime.identifier` in example.yaml; `just lint`
    fails if the two disagree. Set it to python3.15 plus
    allow_preview_runtime = true to try the preview runtime.
  EOT
  type        = string
  default     = "python3.14"
}

variable "allow_preview_runtime" {
  description = "Permit a public-preview runtime, which has no SLA and no support."
  type        = bool
  default     = false
}

variable "architecture" {
  description = "arm64 or x86_64."
  type        = string
  default     = "arm64"
}

variable "memory_mb" {
  description = <<-EOT
    Function memory, which also sets its CPU share. Higher than generic-s3's
    default because this parses every line with a regex rather than passing it
    through, so the work is CPU-bound rather than I/O-bound.
  EOT
  type        = number
  default     = 1024
}

variable "timeout_seconds" {
  description = <<-EOT
    Invocation timeout. AEM forwards on a schedule, so one object is a whole
    hour or day of one log type from one tier - the sample author-tier day held
    46k request-log lines in 5.6 MB, and a busy publish tier is an order of
    magnitude more.
  EOT
  type        = number
  default     = 600
}

variable "reserved_concurrency" {
  description = <<-EOT
    Concurrency cap, or -1 for unreserved. Set a real number before pointing
    this at a bucket with a large existing backlog: an unbounded fan-out will
    hit Loki's per-tenant rate limit and burn the retry budget on 429s.
  EOT
  type        = number
  default     = -1
}

variable "batch_max_lines" {
  description = "Log lines per Loki push."
  type        = number
  default     = 5000

  validation {
    condition     = var.batch_max_lines >= 1 && var.batch_max_lines == floor(var.batch_max_lines)
    error_message = "batch_max_lines must be a positive whole number."
  }
}

variable "batch_max_bytes" {
  description = "Approximate bytes per Loki push."
  type        = number
  default     = 4194304

  validation {
    condition     = var.batch_max_bytes >= 1024 && var.batch_max_bytes == floor(var.batch_max_bytes)
    error_message = "batch_max_bytes must be a whole number of at least 1024."
  }
}

# --- Event source and observability ------------------------------------------

variable "use_sqs" {
  description = "Put an SQS queue between the bucket and the function. See the module docs for the trade-off."
  type        = bool
  default     = true
}

variable "manage_bucket_notification" {
  description = <<-EOT
    Set false when something else already manages the bucket's notification
    configuration. Worth checking here: the bucket is Adobe's forwarding
    destination and may already have a notification set up by whatever created it.
  EOT
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function's own output."
  type        = number
  default     = 30
}

variable "alarm_actions" {
  description = "SNS topic ARNs for the alarms. An alarm with no action is decoration."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to everything this creates."
  type        = map(string)
  default     = {}
}
