# adobe-aem: Adobe Experience Manager Cloud Service logs to Grafana Cloud Loki.
#
# Structurally identical to generic-s3 - the same three shared modules and the
# same source-read policy - and differs only in the environment variables that
# configure the AEM parsing and labelling. `just package adobe-aem` rewrites the
# module sources to ./modules/<name> in the release bundle, so a downloaded zip
# applies with no path escaping its own directory.

locals {
  # The packager resolves lambda_zip_path relative to this directory, so both
  # the in-repo build and the release bundle work without configuration.
  zip_path   = abspath("${path.module}/${var.lambda_zip_path}")
  zip_exists = fileexists(local.zip_path)

  environment_variables = merge(
    {
      GRAFANA_CLOUD_LOKI_ENDPOINT         = var.grafana_cloud_loki_endpoint
      GRAFANA_CLOUD_LOKI_TENANT_ID        = var.grafana_cloud_loki_tenant_id
      GRAFANA_CLOUD_CREDENTIALS_SECRET_ID = module.credentials.secret_name

      SERVICE_NAME      = var.service_name
      SOURCE_KEY_SUFFIX = var.source_suffix

      # How tier, log type and the environment coordinates are read off an
      # object key. Adobe does not document one S3 layout, so this is the knob
      # that adapts the example to a real bucket. Empty keeps the built-in
      # default, which matches `<tier>_<logtype>_<date>.log` under any prefix.
      KEY_PATTERN = var.key_pattern

      # Labels for the coordinates the key does not carry. In practice the
      # program and environment ids come from here, because they are stable per
      # deployment and Adobe does not put them in the object name.
      AEM_PROGRAM_ID = var.aem_program_id
      AEM_ENV_ID     = var.aem_env_id
      AEM_ENV_TYPE   = var.aem_env_type
      AEM_TIER       = var.aem_tier

      LINE_CONTENT           = var.line_content
      SNIFF_CONTENT          = tostring(var.sniff_content)
      CORRELATE_REQUESTS     = tostring(var.correlate_requests)
      MAX_PENDING_REQUESTS   = tostring(var.max_pending_requests)
      DROP_CLIENT_IP         = tostring(var.drop_client_ip)
      LOG_UTC_OFFSET_SECONDS = tostring(var.log_utc_offset_seconds)

      MAX_TIMESTAMP_AGE_SECONDS = tostring(var.max_timestamp_age_seconds)

      LOKI_STATIC_LABELS   = jsonencode(var.static_labels)
      LOKI_BATCH_MAX_LINES = tostring(var.batch_max_lines)
      LOKI_BATCH_MAX_BYTES = tostring(var.batch_max_bytes)

      LOG_LEVEL = var.log_level
    },
    var.extra_environment_variables,
  )
}

# Fails plan with a message that says what to do, rather than letting
# filebase64sha256 error out from inside a module. A guard resource rather than a
# variable validation because a validation block cannot reference path.module.
resource "terraform_data" "deployment_package_present" {
  lifecycle {
    precondition {
      condition = local.zip_exists
      error_message = join(" ", [
        "No deployment package at ${local.zip_path}.",
        "In the release bundle it ships beside this module, so this should not happen.",
        "In the repository, run `just package adobe-aem` first, or point",
        "lambda_zip_path at a zip you have built.",
      ])
    }
  }
}

module "function" {
  source = "../../../common/terraform/modules/lambda-function"

  name        = var.name
  description = "Ships Adobe AEM Cloud Service logs from s3://${var.source_bucket_name} to Grafana Cloud Loki"

  runtime               = var.runtime
  allow_preview_runtime = var.allow_preview_runtime
  architecture          = var.architecture
  handler               = "adobe_aem.handler.lambda_handler"

  filename = local.zip_path
  # Without the hash, a rebuilt zip at the same path is invisible to Terraform:
  # `apply` reports no changes and the old code keeps running.
  source_code_hash = local.zip_exists ? filebase64sha256(local.zip_path) : null

  memory_mb            = var.memory_mb
  timeout_seconds      = var.timeout_seconds
  reserved_concurrency = var.reserved_concurrency

  environment_variables  = local.environment_variables
  additional_policy_json = [data.aws_iam_policy_document.read_source_objects.json]

  log_retention_days = var.log_retention_days
  alarm_actions      = var.alarm_actions
  tags               = var.tags

  depends_on = [terraform_data.deployment_package_present]
}

# Read-only, and scoped to the configured prefix rather than the whole bucket.
# This function never writes to or deletes from the source bucket, which matters
# more than usual here: the bucket is Adobe's log-forwarding destination and
# deleting an object would lose the only copy.
data "aws_iam_policy_document" "read_source_objects" {
  statement {
    sid       = "ReadSourceObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["arn:aws:s3:::${var.source_bucket_name}/${var.source_prefix}*"]
  }

  statement {
    sid       = "ListSourceBucketPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.source_bucket_name}"]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.source_prefix}*"]
    }
  }
}

module "credentials" {
  source = "../../../common/terraform/modules/grafana-cloud-credentials"

  secret_id   = var.credentials_secret_id
  kms_key_arn = var.credentials_kms_key_arn
  role_id     = module.function.role_id
}

module "event_source" {
  source = "../../../common/terraform/modules/s3-event-source"

  name        = var.name
  bucket_name = var.source_bucket_name

  function_arn             = module.function.function_arn
  function_name            = module.function.function_name
  function_timeout_seconds = var.timeout_seconds

  use_sqs                    = var.use_sqs
  manage_bucket_notification = var.manage_bucket_notification
  filter_prefix              = var.source_prefix
  filter_suffix              = var.source_suffix

  alarm_actions = var.alarm_actions
  tags          = var.tags
}

# The queue-to-function permission belongs here rather than in the event-source
# module: the role is owned by the function module, and a module that both
# creates a queue and mutates another module's role is harder to reuse.
data "aws_iam_policy_document" "consume_queue" {
  count = var.use_sqs ? 1 : 0

  statement {
    sid    = "ConsumeNotificationQueue"
    effect = "Allow"

    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]

    resources = [module.event_source.queue_arn]
  }
}

resource "aws_iam_role_policy" "consume_queue" {
  count = var.use_sqs ? 1 : 0

  name   = "consume-notification-queue"
  role   = module.function.role_id
  policy = data.aws_iam_policy_document.consume_queue[0].json
}
