# Base Lambda function: execution role, log group, function, alarms.
#
# Every example's Lambda goes through here, so an improvement to the defaults -
# a finite log retention, a preview-runtime guard, an alarm that actually has an
# action - reaches every example at its next release rather than being fixed
# once and forgotten.

locals {
  # Preview runtimes are gated rather than banned. A caller that wants one has
  # to say so explicitly, which puts the "no SLA, no support" decision in the
  # caller's code where a reviewer will see it.
  preview_runtimes = ["python3.15", "nodejs26.x"]
  is_preview       = contains(local.preview_runtimes, var.runtime)

  log_group_name = "/aws/lambda/${var.name}"

  tags = merge(
    {
      "grafana-cloud-reference-example" = "true"
      "ManagedBy"                       = "terraform"
    },
    var.tags,
  )
}

# --- Execution role ----------------------------------------------------------

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = "${var.name}-role"
  description        = "Execution role for ${var.name}"
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
  tags               = local.tags
}

# Written out rather than using AWSLambdaBasicExecutionRole, which grants
# logs:CreateLogGroup on every log group in the account. This module creates the
# log group itself, so the function only needs to write to that one stream.
data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "logs"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.logs.json
}

resource "aws_iam_role_policy" "additional" {
  count = length(var.additional_policy_json)

  name   = "additional-${count.index}"
  role   = aws_iam_role.this.id
  policy = var.additional_policy_json[count.index]
}

resource "aws_iam_role_policy_attachment" "managed" {
  for_each = toset(var.managed_policy_arns)

  role       = aws_iam_role.this.name
  policy_arn = each.value
}

# A VPC-attached function needs these to create and tear down its ENIs. Attached
# only when a VPC is configured, so a non-VPC function does not carry the
# account-wide ec2:* permissions this managed policy grants.
resource "aws_iam_role_policy_attachment" "vpc_access" {
  count = var.vpc_config == null ? 0 : 1

  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "dlq" {
  count = var.dead_letter_target_arn == null ? 0 : 1

  statement {
    sid       = "PublishToDeadLetterTarget"
    effect    = "Allow"
    actions   = startswith(coalesce(var.dead_letter_target_arn, ""), "arn:aws:sns:") ? ["sns:Publish"] : ["sqs:SendMessage"]
    resources = [var.dead_letter_target_arn]
  }
}

resource "aws_iam_role_policy" "dlq" {
  count = var.dead_letter_target_arn == null ? 0 : 1

  name   = "dead-letter"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.dlq[0].json
}

# --- Log group ---------------------------------------------------------------

# Created explicitly rather than letting Lambda create it on first invocation.
# An implicitly created log group has no retention (never expires) and no tags,
# and Terraform never learns it exists, so it survives a `destroy`.
resource "aws_cloudwatch_log_group" "this" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_group_kms_key_arn
  tags              = local.tags
}

# --- Function ----------------------------------------------------------------

resource "aws_lambda_function" "this" {
  function_name = var.name
  description   = var.description
  role          = aws_iam_role.this.arn

  package_type  = var.package_type
  filename      = var.filename
  s3_bucket     = var.s3_bucket
  s3_key        = var.s3_key
  image_uri     = var.image_uri
  handler       = var.package_type == "Zip" ? var.handler : null
  runtime       = var.package_type == "Zip" ? var.runtime : null
  architectures = [var.architecture]

  source_code_hash = var.source_code_hash

  # Publishing a numbered version on every code change is what makes
  # function_qualified_arn point at something immutable, and what makes a
  # rollback possible without rebuilding. Off by default because each version
  # counts against the account's code-storage quota.
  publish = var.publish

  memory_size                    = var.memory_mb
  timeout                        = var.timeout_seconds
  reserved_concurrent_executions = var.reserved_concurrency

  ephemeral_storage {
    size = var.ephemeral_storage_mb
  }

  dynamic "environment" {
    for_each = length(var.environment_variables) > 0 ? [1] : []

    content {
      variables = var.environment_variables
    }
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config == null ? [] : [var.vpc_config]

    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  dynamic "dead_letter_config" {
    for_each = var.dead_letter_target_arn == null ? [] : [var.dead_letter_target_arn]

    content {
      target_arn = dead_letter_config.value
    }
  }

  dynamic "tracing_config" {
    for_each = var.tracing_mode == null ? [] : [var.tracing_mode]

    content {
      mode = tracing_config.value
    }
  }

  tags = local.tags

  # A lifecycle precondition rather than a variable validation block, because
  # the rule spans two variables and a validation block only sees its own.
  lifecycle {
    precondition {
      condition = !local.is_preview || var.allow_preview_runtime
      error_message = join(" ", [
        "Runtime ${var.runtime} is in public preview: no Lambda SLA, no technical",
        "support, and AWS advises against production use. Set",
        "allow_preview_runtime = true to proceed deliberately, or pin a GA runtime.",
      ])
    }

    # Exactly one source, not at least one. Passing both filename and
    # s3_bucket/s3_key is accepted here but rejected by the API later, with a
    # message that does not say which one it ignored.
    precondition {
      condition = (
        var.package_type == "Image"
        ? (var.image_uri != null && var.filename == null && var.s3_bucket == null && var.s3_key == null)
        : (
          var.image_uri == null && var.handler != null &&
          (
            (var.filename != null && var.s3_bucket == null && var.s3_key == null) ||
            (var.filename == null && var.s3_bucket != null && var.s3_key != null)
          )
        )
      )
      error_message = join(" ", [
        "Specify exactly one deployment source. A Zip function needs a handler plus",
        "either filename, or both s3_bucket and s3_key, and no image_uri. An Image",
        "function needs image_uri and none of filename, s3_bucket or s3_key.",
      ])
    }
  }

  # The log group must exist first, or Lambda creates an unmanaged one with no
  # retention on the first invocation and the two then fight.
  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy.logs,
  ]
}

# --- Alarms ------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "errors" {
  count = var.create_alarms ? 1 : 0

  alarm_name          = "${var.name}-errors"
  alarm_description   = "${var.name} returned an error. Check the log group ${local.log_group_name}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.error_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # Lambda publishes no datapoint when there are no invocations at all, so the
  # default missing-data behaviour would leave the alarm in INSUFFICIENT_DATA
  # forever on a quiet bucket.
  treat_missing_data = "notBreaching"

  dimensions    = { FunctionName = aws_lambda_function.this.function_name }
  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
  tags          = local.tags
}

resource "aws_cloudwatch_metric_alarm" "throttles" {
  count = var.create_alarms ? 1 : 0

  alarm_name          = "${var.name}-throttles"
  alarm_description   = "${var.name} was throttled: concurrency is capped below demand."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions    = { FunctionName = aws_lambda_function.this.function_name }
  alarm_actions = var.alarm_actions
  ok_actions    = var.alarm_actions
  tags          = local.tags
}
