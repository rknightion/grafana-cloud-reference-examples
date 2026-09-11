variable "name" {
  description = "Name for the function and its companion resources."
  type        = string
  default     = "grafana-cloud-generic-s3"
}

variable "source_bucket_name" {
  description = "Bucket to read from. Not created here; only s3:GetObject is granted on it."
  type        = string
}

variable "source_prefix" {
  description = "Only process keys under this prefix. Empty means the whole bucket."
  type        = string
  default     = ""
}

variable "source_suffix" {
  description = "Only process keys with this suffix, e.g. .gz. Empty means any."
  type        = string
  default     = ""
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
    Extra Loki stream labels applied to every line, e.g. { env = "prod" }.

    Low cardinality only. A label whose value varies per object or per record
    creates one Loki stream per value; the function rejects such a label rather
    than letting you discover it on the bill.
  EOT
  type        = map(string)
  default     = {}
}

# --- Function behaviour ------------------------------------------------------

variable "service_name" {
  description = "Value of the service_name label."
  type        = string
  default     = "generic-s3"
}

variable "record_format" {
  description = "auto, lines, jsonl, json_array or csv. auto infers from the object key."
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "lines", "jsonl", "json_array", "csv"], var.record_format)
    error_message = "record_format must be auto, lines, jsonl, json_array or csv."
  }
}

variable "prefix_label_depth" {
  description = <<-EOT
    Leading key segments exposed as a `prefix` label. 0 disables it.

    Use 1 or 2 for a bucket laid out as <team>/<source>/<date>/... Never deep
    enough to reach a date or a filename, which is one stream per file.
  EOT
  type        = number
  default     = 0

  # `type = number` accepts 1.5, which renders as "1.5" in the environment and
  # then fails at cold start with a ValueError the operator has to go and read
  # the logs to find.
  validation {
    condition     = var.prefix_label_depth >= 0 && var.prefix_label_depth == floor(var.prefix_label_depth)
    error_message = "prefix_label_depth must be a non-negative whole number."
  }
}

variable "timestamp_field" {
  description = "JSON field to read the event time from. Empty means stamp with ingestion time."
  type        = string
  default     = ""
}

variable "timestamp_format" {
  description = "rfc3339, epoch_s, epoch_ms, epoch_us, epoch_ns, or a Python strptime pattern."
  type        = string
  default     = "rfc3339"
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
    `just package generic-s3` put it, so no configuration is needed in either case.
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
  description = "Function memory, which also sets its CPU share."
  type        = number
  default     = 512
}

variable "timeout_seconds" {
  description = "Invocation timeout. Raise it for very large objects."
  type        = number
  default     = 300
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
  description = "Set false when something else already manages the bucket's notification configuration."
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
