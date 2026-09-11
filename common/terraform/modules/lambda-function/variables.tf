variable "name" {
  description = "Function name. Also the prefix for the role, log group and alarms."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9-_]{0,63}$", var.name))
    error_message = "name must be 1-64 characters of letters, digits, hyphen or underscore."
  }
}

variable "description" {
  description = "Human-readable description shown in the Lambda console."
  type        = string
  default     = ""
}

# --- Runtime -----------------------------------------------------------------
# The runtime is a variable with an allowlist, never a hardcoded string, so
# bumping it later is a one-line change with a validated value rather than a
# find-and-replace across the repo. `just lint` checks this default against the
# owning example's example.yaml so the two cannot drift.

variable "runtime" {
  description = <<-EOT
    Lambda runtime identifier. Defaults to the longest-lived GA managed runtime.

    Deprecation dates as published by AWS, for planning only:
      python3.14      2029-06-30
      python3.13      2029-06-30
      nodejs24.x      2028-04-30
      nodejs22.x      2027-04-30
      provided.al2023 2029-06-30   (OS clock, not a language clock - best for Go/Rust)
      python3.15      preview, not scheduled
      nodejs26.x      preview, not scheduled

    Check the live table before relying on any of these:
    https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html
  EOT
  type        = string
  default     = "python3.14"

  validation {
    condition = contains([
      "python3.15", "python3.14", "python3.13", "python3.12",
      "nodejs26.x", "nodejs24.x", "nodejs22.x",
      "provided.al2023",
    ], var.runtime)
    error_message = "runtime must be a currently supported Amazon Linux 2023 runtime."
  }
}

variable "allow_preview_runtime" {
  description = <<-EOT
    Permit a public-preview runtime. Preview runtimes carry no Lambda SLA and no
    technical support, and AWS says not to use them for production workloads, so
    this is opt-in rather than the default.
  EOT
  type        = bool
  default     = false
}

variable "architecture" {
  description = "arm64 or x86_64. arm64 is cheaper per GB-second and is the default."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

# --- Code --------------------------------------------------------------------

variable "package_type" {
  description = "Zip or Image. Image expects image_uri instead of a zip."
  type        = string
  default     = "Zip"

  validation {
    condition     = contains(["Zip", "Image"], var.package_type)
    error_message = "package_type must be Zip or Image."
  }
}

variable "filename" {
  description = "Path to a local deployment zip. Mutually exclusive with s3_bucket/s3_key."
  type        = string
  default     = null
}

variable "s3_bucket" {
  description = "Bucket holding the deployment zip. Use with s3_key."
  type        = string
  default     = null
}

variable "s3_key" {
  description = "Key of the deployment zip within s3_bucket."
  type        = string
  default     = null
}

variable "image_uri" {
  description = "ECR image URI. Required when package_type is Image."
  type        = string
  default     = null
}

variable "handler" {
  description = "Entrypoint, e.g. generic_s3.handler.lambda_handler. Unused for Image."
  type        = string
  default     = null
}

variable "publish" {
  description = <<-EOT
    Publish a numbered Lambda version on every code change.

    Required for function_qualified_arn to mean anything: without it, the
    qualified ARN resolves to $LATEST, which is mutable, so an alias or an event
    source mapping pinned to it is not actually pinned. Off by default because
    every published version counts against the account's code-storage quota and
    versions are never deleted automatically.
  EOT
  type        = bool
  default     = false
}

variable "source_code_hash" {
  description = <<-EOT
    Base64 SHA-256 of the deployment package. Without it Terraform cannot tell
    that a rebuilt zip with the same key is new code, so `apply` reports no
    changes and the old code keeps running.
  EOT
  type        = string
  default     = null
}

# --- Sizing ------------------------------------------------------------------

variable "memory_mb" {
  description = <<-EOT
    Memory, which also sets the CPU share. Streaming work is rarely
    memory-bound, so raise this to buy CPU for decompression rather than to
    hold more data.
  EOT
  type        = number
  default     = 512

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Invocation timeout. Must be at least the source queue's batch window plus headroom."
  type        = number
  default     = 300

  validation {
    condition     = var.timeout_seconds >= 1 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 1 and 900."
  }
}

variable "ephemeral_storage_mb" {
  description = "Size of /tmp. Only raise it if the function actually writes to disk."
  type        = number
  default     = 512
}

variable "reserved_concurrency" {
  description = <<-EOT
    Maximum concurrent executions, or -1 for unreserved. Set a real number when
    the function writes to a rate-limited destination: an unbounded fan-out from
    a large S3 prefix will otherwise hit Loki's per-tenant rate limit and spend
    the whole retry budget on 429s.
  EOT
  type        = number
  default     = -1
}

# --- Configuration and permissions -------------------------------------------

variable "environment_variables" {
  description = <<-EOT
    Environment variables for the function. Never put a credential here: the
    values are readable by anyone with lambda:GetFunctionConfiguration and are
    stored in Terraform state in plaintext. Pass a Secrets Manager id instead.
  EOT
  type        = map(string)
  default     = {}
}

variable "additional_policy_json" {
  description = "Extra IAM policy documents to attach to the execution role, as JSON strings."
  type        = list(string)
  default     = []
}

variable "managed_policy_arns" {
  description = "Managed policy ARNs to attach. Keep this empty unless there is a real reason."
  type        = list(string)
  default     = []
}

variable "vpc_config" {
  description = <<-EOT
    Attach the function to a VPC. Leave null unless the function must reach a
    private resource: a VPC-attached function needs a NAT gateway or interface
    endpoints to reach S3, Secrets Manager and Grafana Cloud, all of which cost
    money and are a common cause of a function that times out with no log line.
  EOT
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "dead_letter_target_arn" {
  description = "SQS or SNS ARN for asynchronous invocation failures. Null disables the DLQ."
  type        = string
  default     = null
}

# --- Observability -----------------------------------------------------------

variable "log_retention_days" {
  description = <<-EOT
    CloudWatch Logs retention. 0 means never expire, which is a slow-growing
    bill nobody notices; the default is deliberately finite.
  EOT
  type        = number
  default     = 30

  validation {
    condition = contains(
      [0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_days
    )
    error_message = "log_retention_days must be one of the values CloudWatch Logs accepts."
  }
}

variable "log_group_kms_key_arn" {
  description = "KMS key for log group encryption. Null uses the CloudWatch-managed key."
  type        = string
  default     = null
}

variable "tracing_mode" {
  description = "X-Ray tracing: Active, PassThrough, or null to disable."
  type        = string
  default     = null

  validation {
    condition     = var.tracing_mode == null || contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be Active, PassThrough, or null."
  }
}

variable "create_alarms" {
  description = "Create the CloudWatch alarms for errors and throttles."
  type        = bool
  default     = true
}

variable "alarm_actions" {
  description = "SNS topic ARNs notified when an alarm fires. An alarm with no action is decoration."
  type        = list(string)
  default     = []
}

variable "error_alarm_threshold" {
  description = "Function errors in a 5-minute period that trip the alarm."
  type        = number
  default     = 1
}

variable "tags" {
  description = "Tags applied to every resource this module creates."
  type        = map(string)
  default     = {}
}
