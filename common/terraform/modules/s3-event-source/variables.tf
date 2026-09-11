variable "name" {
  description = "Prefix for the queue, dead-letter queue and alarm names."
  type        = string
}

variable "bucket_name" {
  description = <<-EOT
    Source bucket. Not created here - this module only reads from it and
    subscribes to its notifications.
  EOT
  type        = string
}

variable "function_arn" {
  description = "ARN of the Lambda function that consumes the events."
  type        = string
}

variable "function_name" {
  description = "Name of that function, for the alarm dimension and the direct-invoke permission."
  type        = string
}

variable "function_timeout_seconds" {
  description = <<-EOT
    The function's configured timeout. Used to derive the queue visibility
    timeout, which AWS requires to be at least as long - a shorter one lets SQS
    redeliver a message while the first invocation is still processing it, which
    duplicates every line it has already shipped.
  EOT
  type        = number
}

variable "use_sqs" {
  description = <<-EOT
    Put an SQS queue between the bucket and the function.

    true (default) gives batching, a visibility timeout you control, a retry
    policy, a dead-letter queue you can inspect and redrive, and partial batch
    failure so one unreadable object does not force redelivery of the whole
    batch.

    false invokes the function directly from the bucket notification: simpler,
    one invocation per object, two retries and then the event is gone.
  EOT
  type        = bool
  default     = true
}

variable "filter_prefix" {
  description = "Only notify for keys under this prefix. Empty means the whole bucket."
  type        = string
  default     = ""
}

variable "filter_suffix" {
  description = "Only notify for keys with this suffix, e.g. .gz. Empty means any."
  type        = string
  default     = ""
}

variable "event_types" {
  description = "S3 event types to subscribe to."
  type        = list(string)
  default     = ["s3:ObjectCreated:*"]
}

variable "manage_bucket_notification" {
  description = <<-EOT
    Create the aws_s3_bucket_notification for the source bucket.

    Set this to false when the bucket already has a notification configuration
    managed elsewhere. The S3 API replaces a bucket's ENTIRE notification
    configuration on every write, so two Terraform resources pointing at one
    bucket silently delete each other's subscriptions on alternating applies.
    With this false, subscribe the queue yourself in whichever configuration
    already owns the bucket; `queue_arn` is an output for exactly that.
  EOT
  type        = bool
  default     = true
}

variable "batch_size" {
  description = "Maximum S3 notifications per invocation. 1 is safest; raise it for many small objects."
  type        = number
  default     = 10

  validation {
    condition     = var.batch_size >= 1 && var.batch_size <= 10000
    error_message = "batch_size must be between 1 and 10000."
  }
}

variable "maximum_batching_window_seconds" {
  description = "How long SQS waits to fill a batch. 0 means invoke as soon as a message arrives."
  type        = number
  default     = 30
}

variable "max_receive_count" {
  description = "Delivery attempts before a message goes to the dead-letter queue."
  type        = number
  default     = 3
}

variable "message_retention_seconds" {
  description = "How long an undelivered message survives. 14 days is the SQS maximum."
  type        = number
  default     = 345600
}

variable "dlq_message_retention_seconds" {
  description = "Dead-letter retention. The maximum, so there is time to diagnose and redrive."
  type        = number
  default     = 1209600
}

variable "kms_master_key_id" {
  description = <<-EOT
    KMS key for queue encryption. Null uses SQS-managed encryption (SSE-SQS),
    which is free and enabled here by default. A customer-managed key also
    requires S3 to be granted kms:GenerateDataKey on it, or every notification
    is silently rejected.
  EOT
  type        = string
  default     = null
}

variable "create_alarms" {
  description = "Create the dead-letter-queue depth and queue-age alarms."
  type        = bool
  default     = true
}

variable "alarm_actions" {
  description = "SNS topics notified when an alarm fires."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource this module creates."
  type        = map(string)
  default     = {}
}
