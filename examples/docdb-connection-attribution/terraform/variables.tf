variable "name" {
  description = "Name for the function and its companion resources."
  type        = string
  default     = "grafana-cloud-docdb-connection-attribution"

  validation {
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9-_]{0,63}$", var.name))
    error_message = "name must be 1-64 characters of letters, digits, hyphen or underscore."
  }
}

# --- DocumentDB ---------------------------------------------------------------

variable "docdb_cluster_id" {
  description = <<-EOT
    DocumentDB cluster identifier (not the ARN), e.g. the value after
    `cluster:` in the cluster's ARN. Used for rds:Describe* and, unless
    audit_log_group overrides it, to derive the audit log group name.
  EOT
  type        = string

  validation {
    condition     = length(trimspace(var.docdb_cluster_id)) > 0
    error_message = "docdb_cluster_id must not be empty."
  }
}

variable "docdb_credentials_secret_id" {
  description = <<-EOT
    Name or ARN of an existing Secrets Manager secret holding the DocumentDB
    credentials, as JSON `{"username": ..., "password": ...}`. Create it
    before applying; the credentials are never a Terraform input, so they
    never land in state.
  EOT
  type        = string
}

variable "docdb_credentials_kms_key_arn" {
  description = "KMS key ARN if the DocumentDB credentials secret uses a customer-managed key. Null for the default key."
  type        = string
  default     = null
}

variable "docdb_security_group_id" {
  description = <<-EOT
    Security group id attached to the DocumentDB cluster's instances. When
    set, the stack adds an ingress rule on it allowing tcp/27017 from the
    function's own security group. Leave null to add that rule yourself.
  EOT
  type        = string
  default     = null
}

variable "audit_log_group" {
  description = <<-EOT
    CloudWatch log group holding the DocumentDB cluster's audit log export.
    Null defaults to `/aws/docdb/<docdb_cluster_id>/audit`, which is where
    DocumentDB writes it when audit log export to CloudWatch Logs is enabled.
  EOT
  type        = string
  default     = null
}

variable "audit_backfill_minutes" {
  description = "Minutes of audit log history to read on the function's first run."
  type        = number
  default     = 10080

  validation {
    condition     = var.audit_backfill_minutes >= 1 && var.audit_backfill_minutes == floor(var.audit_backfill_minutes)
    error_message = "audit_backfill_minutes must be a positive whole number."
  }
}

variable "mapping_ttl_days" {
  description = "How long a client-address-to-user mapping is kept in the attribution table before it expires."
  type        = number
  default     = 14

  validation {
    condition     = var.mapping_ttl_days >= 1 && var.mapping_ttl_days == floor(var.mapping_ttl_days)
    error_message = "mapping_ttl_days must be a positive whole number."
  }
}

variable "include_client_address" {
  description = "Add the client host (no port) as the client.address attribute. Off by default: it multiplies series by the number of client hosts."
  type        = bool
  default     = false
}

# --- Grafana Cloud -------------------------------------------------------------

variable "grafana_cloud_otlp_endpoint" {
  description = "Grafana Cloud OTLP endpoint, e.g. https://otlp-gateway-prod-eu-west-2.grafana.net/otlp"
  type        = string

  validation {
    condition     = startswith(var.grafana_cloud_otlp_endpoint, "https://")
    error_message = "grafana_cloud_otlp_endpoint must start with https://."
  }
}

variable "grafana_cloud_tenant_id" {
  description = <<-EOT
    Numeric Grafana Cloud stack (instance) id. Only needed when the
    credentials secret holds a bare token rather than a JSON object with its
    own tenant_id field.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = var.grafana_cloud_tenant_id == "" || can(regex("^[0-9]+$", var.grafana_cloud_tenant_id))
    error_message = "grafana_cloud_tenant_id must be empty or numeric."
  }
}

variable "credentials_secret_id" {
  description = <<-EOT
    Name or ARN of an existing Secrets Manager secret holding the Grafana
    Cloud Cloud Access Policy token, as JSON `{"tenant_id": ..., "token":
    "glc_..."}` or a bare token string. Create it before applying; the token
    is never a Terraform input, so it never lands in state.
  EOT
  type        = string
}

variable "credentials_kms_key_arn" {
  description = "KMS key ARN if the Grafana Cloud credentials secret uses a customer-managed key. Null for the default key."
  type        = string
  default     = null
}

# --- Network ---------------------------------------------------------------

variable "subnet_ids" {
  description = "Private subnet ids the function runs in. Must have a route to a NAT gateway or the required VPC interface/gateway endpoints."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "subnet_ids must not be empty."
  }
}

# --- Function behaviour ------------------------------------------------------

variable "schedule_expression" {
  description = "EventBridge schedule expression that invokes the function. The event payload is ignored."
  type        = string
  default     = "rate(1 minute)"

  validation {
    condition     = length(trimspace(var.schedule_expression)) > 0
    error_message = "schedule_expression must not be empty."
  }
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
    Path to the deployment zip. The default is where both the release bundle
    and `just package docdb-connection-attribution` put it, so no
    configuration is needed in either case.
  EOT
  type        = string
  default     = "lambda.zip"
}

variable "runtime" {
  description = <<-EOT
    Lambda runtime. Must match `runtime.identifier` in example.yaml; `just
    lint` fails if the two disagree. Set it to python3.15 plus
    allow_preview_runtime = true to try the preview runtime.
  EOT
  type        = string
  default     = "python3.14"

  validation {
    # The package requires Python 3.14 or later.
    condition     = contains(["python3.14", "python3.15"], var.runtime)
    error_message = "runtime must be python3.14, or python3.15 with allow_preview_runtime = true."
  }
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

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "memory_mb" {
  description = "Function memory, which also sets its CPU share."
  type        = number
  default     = 256

  validation {
    condition     = var.memory_mb >= 128 && var.memory_mb <= 10240
    error_message = "memory_mb must be between 128 and 10240."
  }
}

variable "timeout_seconds" {
  description = "Invocation timeout. Must comfortably exceed one join-and-export run against the cluster's audit log."
  type        = number
  default     = 60

  validation {
    condition     = var.timeout_seconds >= 1 && var.timeout_seconds <= 900
    error_message = "timeout_seconds must be between 1 and 900."
  }
}

# --- Observability -----------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the function's own output."
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
