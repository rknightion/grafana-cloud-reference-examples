# Wires an S3 bucket's object-created notifications to a Lambda function, with
# an SQS queue in between by default.

data "aws_caller_identity" "current" {}

locals {
  tags = merge(
    {
      "grafana-cloud-reference-example" = "true"
      "ManagedBy"                       = "terraform"
    },
    var.tags,
  )

  bucket_arn = "arn:aws:s3:::${var.bucket_name}"

  # AWS requires the visibility timeout to be at least the function timeout and
  # recommends six times it, so a retried invocation does not race a redelivery.
  visibility_timeout = var.function_timeout_seconds * 6
}

# --- Queues ------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  count = var.use_sqs ? 1 : 0

  name                      = "${var.name}-dlq"
  message_retention_seconds = var.dlq_message_retention_seconds
  sqs_managed_sse_enabled   = var.kms_master_key_id == null
  kms_master_key_id         = var.kms_master_key_id
  tags                      = local.tags
}

resource "aws_sqs_queue" "this" {
  count = var.use_sqs ? 1 : 0

  name                       = var.name
  visibility_timeout_seconds = local.visibility_timeout
  message_retention_seconds  = var.message_retention_seconds
  sqs_managed_sse_enabled    = var.kms_master_key_id == null
  kms_master_key_id          = var.kms_master_key_id

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[0].arn
    maxReceiveCount     = var.max_receive_count
  })

  tags = local.tags
}

# The queue policy is scoped to this bucket in this account. Without the
# SourceAccount and SourceArn conditions, any S3 bucket in any AWS account could
# post to this queue - the confused-deputy problem the service principal alone
# does not prevent.
data "aws_iam_policy_document" "queue" {
  count = var.use_sqs ? 1 : 0

  statement {
    sid       = "AllowS3Notifications"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.this[0].arn]

    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.bucket_arn]
    }
  }
}

resource "aws_sqs_queue_policy" "this" {
  count = var.use_sqs ? 1 : 0

  queue_url = aws_sqs_queue.this[0].id
  policy    = data.aws_iam_policy_document.queue[0].json
}

# --- Bucket notification -----------------------------------------------------

# S3 replaces a bucket's whole notification configuration on every write, so
# this resource must be the only thing managing it. See
# manage_bucket_notification for the shared-bucket case.
resource "aws_s3_bucket_notification" "this" {
  count = var.manage_bucket_notification ? 1 : 0

  bucket = var.bucket_name

  dynamic "queue" {
    for_each = var.use_sqs ? [1] : []

    content {
      id            = var.name
      queue_arn     = aws_sqs_queue.this[0].arn
      events        = var.event_types
      filter_prefix = var.filter_prefix
      filter_suffix = var.filter_suffix
    }
  }

  dynamic "lambda_function" {
    for_each = var.use_sqs ? [] : [1]

    content {
      id                  = var.name
      lambda_function_arn = var.function_arn
      events              = var.event_types
      filter_prefix       = var.filter_prefix
      filter_suffix       = var.filter_suffix
    }
  }

  # S3 validates that it can reach the target when the configuration is written,
  # so the permission has to exist first or the apply fails with an unhelpful
  # "unable to validate the following destination configurations".
  depends_on = [
    aws_sqs_queue_policy.this,
    aws_lambda_permission.allow_s3,
  ]
}

# --- Delivery to the function ------------------------------------------------

resource "aws_lambda_event_source_mapping" "this" {
  count = var.use_sqs ? 1 : 0

  event_source_arn = aws_sqs_queue.this[0].arn
  function_name    = var.function_arn
  enabled          = true

  batch_size                         = var.batch_size
  maximum_batching_window_in_seconds = var.maximum_batching_window_seconds

  # Without this, Lambda ignores the handler's batchItemFailures response and
  # redelivers the entire batch when any one message fails, duplicating every
  # line the successful messages already shipped.
  function_response_types = ["ReportBatchItemFailures"]

  # A standard SQS source only accepts a batch size above 10 when a batching
  # window is set. A variable validation cannot express this: the rule spans two
  # variables.
  lifecycle {
    precondition {
      condition = var.batch_size <= 10 || var.maximum_batching_window_seconds >= 1
      error_message = join(" ", [
        "batch_size ${var.batch_size} exceeds 10, which SQS only allows with a",
        "batching window. Set maximum_batching_window_seconds to at least 1.",
      ])
    }
  }
}

resource "aws_lambda_permission" "allow_s3" {
  count = var.use_sqs ? 0 : 1

  statement_id   = "AllowExecutionFromS3Bucket"
  action         = "lambda:InvokeFunction"
  function_name  = var.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = local.bucket_arn
  source_account = data.aws_caller_identity.current.account_id
}

# --- Alarms ------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  count = var.use_sqs && var.create_alarms ? 1 : 0

  alarm_name        = "${var.name}-dlq-not-empty"
  alarm_description = "Objects failed repeatedly and are parked in ${aws_sqs_queue.dlq[0].name}. Each message holds the original S3 notification, so a redrive reprocesses exactly what failed."

  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions    = { QueueName = aws_sqs_queue.dlq[0].name }
  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "queue_age" {
  count = var.use_sqs && var.create_alarms ? 1 : 0

  alarm_name        = "${var.name}-backlog-ageing"
  alarm_description = "The oldest message in ${aws_sqs_queue.this[0].name} is over an hour old: the function is not keeping up, or is failing without erroring."

  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 3600
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions    = { QueueName = aws_sqs_queue.this[0].name }
  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
  tags          = local.tags
}
