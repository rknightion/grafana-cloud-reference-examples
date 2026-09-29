# docdb-connection-attribution: DocumentDB open connections attributed to
# database users, exported to Grafana Cloud over OTLP.
#
# Shared modules plus the resources specific to this example: the attribution
# table, the function's own security group, and the EventBridge schedule.
# `just package docdb-connection-attribution` rewrites the module sources to
# ./modules/<name> in the release bundle, so a downloaded zip applies with no
# path outside itself.

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  # The packager resolves lambda_zip_path relative to this directory, so both
  # the in-repo build and the release bundle work without configuration.
  zip_path   = abspath("${path.module}/${var.lambda_zip_path}")
  zip_exists = fileexists(local.zip_path)

  # Matches the function's own default so the IAM policy below is correct
  # whether or not audit_log_group is set.
  audit_log_group = coalesce(var.audit_log_group, "/aws/docdb/${var.docdb_cluster_id}/audit")

  tags = merge(
    {
      "grafana-cloud-reference-example" = "true"
      "ManagedBy"                       = "terraform"
    },
    var.tags,
  )

  environment_variables = merge(
    {
      DOCDB_CLUSTER_ID            = var.docdb_cluster_id
      DOCDB_CREDENTIALS_SECRET_ID = module.docdb_credentials.secret_name
      AUDIT_LOG_GROUP             = local.audit_log_group
      AUDIT_BACKFILL_MINUTES      = tostring(var.audit_backfill_minutes)
      ATTRIBUTION_TABLE           = aws_dynamodb_table.attribution.name
      MAPPING_TTL_DAYS            = tostring(var.mapping_ttl_days)

      GRAFANA_CLOUD_OTLP_ENDPOINT         = var.grafana_cloud_otlp_endpoint
      GRAFANA_CLOUD_CREDENTIALS_SECRET_ID = module.credentials.secret_name
      GRAFANA_CLOUD_TENANT_ID             = var.grafana_cloud_tenant_id

      INCLUDE_CLIENT_ADDRESS = tostring(var.include_client_address)

      LOG_LEVEL             = var.log_level
      LOG_DEBUG_SAMPLE_RATE = tostring(var.log_debug_sample_rate)
    },
    var.extra_environment_variables,
  )
}

# Fails plan with a message that says what to do, rather than letting
# filebase64sha256 error out from inside a module. A guard resource rather than
# a variable validation because a validation block cannot reference path.module.
resource "terraform_data" "deployment_package_present" {
  lifecycle {
    precondition {
      condition = local.zip_exists
      error_message = join(" ", [
        "No deployment package at ${local.zip_path}.",
        "In the release bundle it ships beside this module, so this should not happen.",
        "In the repository, run `just package docdb-connection-attribution` first, or",
        "point lambda_zip_path at a zip you have built.",
      ])
    }
  }
}

# --- Network -------------------------------------------------------------------

# Looked up rather than taken as an input: the VPC id is only needed to scope
# the DocumentDB egress rule to the VPC's own CIDR, and deriving it from the
# first subnet means the caller does not have to pass the same VPC twice.
data "aws_subnet" "selected" {
  id = var.subnet_ids[0]
}

data "aws_vpc" "selected" {
  id = data.aws_subnet.selected.vpc_id
}

resource "aws_security_group" "function" {
  name        = "${var.name}-sg"
  description = "${var.name}: no ingress, egress to Grafana Cloud/AWS APIs and to DocumentDB"
  vpc_id      = data.aws_vpc.selected.id
  tags        = local.tags
}

resource "aws_vpc_security_group_egress_rule" "https" {
  security_group_id = aws_security_group.function.id
  description       = "Grafana Cloud, AWS APIs, CA bundle"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  tags              = local.tags
}

# To the cluster's own security group when it is known, which also covers a cluster in a
# secondary VPC CIDR; otherwise to the VPC's primary CIDR.
resource "aws_vpc_security_group_egress_rule" "docdb" {
  security_group_id            = aws_security_group.function.id
  description                  = "DocumentDB"
  referenced_security_group_id = var.docdb_security_group_id
  cidr_ipv4                    = var.docdb_security_group_id == null ? data.aws_vpc.selected.cidr_block : null
  ip_protocol                  = "tcp"
  from_port                    = 27017
  to_port                      = 27017
  tags                         = local.tags
}

# Optional: only created when the caller passes the DocumentDB cluster's own
# security group id. Without this, the customer must add the equivalent rule
# themselves before the function can reach the cluster.
resource "aws_vpc_security_group_ingress_rule" "docdb_from_function" {
  count = var.docdb_security_group_id == null ? 0 : 1

  security_group_id            = var.docdb_security_group_id
  description                  = "Allow ${var.name} to reach DocumentDB"
  referenced_security_group_id = aws_security_group.function.id
  ip_protocol                  = "tcp"
  from_port                    = 27017
  to_port                      = 27017
  tags                         = local.tags
}

# --- Attribution table -----------------------------------------------------

# Derived, disposable data: the client-address-to-user mapping the function
# rebuilds from the audit log on every run. No point-in-time recovery, and a
# TTL so a stale mapping does not accumulate forever.
resource "aws_dynamodb_table" "attribution" {
  name         = "${var.name}-attribution"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  tags = local.tags
}

# --- Function ----------------------------------------------------------------

module "function" {
  source = "../../../common/terraform/modules/lambda-function"

  name        = var.name
  description = "Attributes open DocumentDB connections on ${var.docdb_cluster_id} to database users and exports docdb.connections.open to Grafana Cloud"

  runtime               = var.runtime
  allow_preview_runtime = var.allow_preview_runtime
  architecture          = var.architecture
  handler               = "docdb_connection_attribution.handler.lambda_handler"

  filename = local.zip_path
  # Without the hash, a rebuilt zip at the same path is invisible to Terraform:
  # `apply` reports no changes and the old code keeps running.
  # Guarded so plan fails on the precondition above with a useful message
  # instead of on a function call inside a module.
  source_code_hash = local.zip_exists ? filebase64sha256(local.zip_path) : null

  memory_mb       = var.memory_mb
  timeout_seconds = var.timeout_seconds
  # Fixed, not a variable: the DynamoDB checkpoint is single-writer, so two
  # overlapping runs would race on it. rate(1 minute) plus a slow run is
  # exactly the case this guards against.
  reserved_concurrency = 1

  vpc_config = {
    subnet_ids         = var.subnet_ids
    security_group_ids = [aws_security_group.function.id]
  }

  environment_variables  = local.environment_variables
  additional_policy_json = [data.aws_iam_policy_document.docdb_attribution.json]

  log_retention_days = var.log_retention_days
  alarm_actions      = var.alarm_actions
  tags               = local.tags

  depends_on = [terraform_data.deployment_package_present]
}

# rds:Describe* and logs:FilterLogEvents to read the cluster's current
# connections and its audit log; dynamodb:* scoped to the attribution table.
# secretsmanager access is granted separately by the two credentials modules
# below, so it is not repeated here.
data "aws_iam_policy_document" "docdb_attribution" {
  statement {
    sid       = "DescribeDocDBCluster"
    effect    = "Allow"
    actions   = ["rds:DescribeDBClusters"]
    resources = ["arn:${data.aws_partition.current.partition}:rds:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:cluster:${var.docdb_cluster_id}"]
  }

  statement {
    sid       = "DescribeDocDBInstances"
    effect    = "Allow"
    actions   = ["rds:DescribeDBInstances"]
    resources = ["arn:${data.aws_partition.current.partition}:rds:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:db:*"]
  }

  statement {
    sid       = "ReadAuditLog"
    effect    = "Allow"
    actions   = ["logs:FilterLogEvents"]
    resources = ["arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:${local.audit_log_group}:*"]
  }

  statement {
    sid    = "ReadWriteAttributionTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:BatchGetItem",
    ]
    resources = [aws_dynamodb_table.attribution.arn]
  }
}

module "credentials" {
  source = "../../../common/terraform/modules/grafana-cloud-credentials"

  secret_id   = var.credentials_secret_id
  kms_key_arn = var.credentials_kms_key_arn
  role_id     = module.function.role_id
}

module "docdb_credentials" {
  source = "../../../common/terraform/modules/grafana-cloud-credentials"

  secret_id   = var.docdb_credentials_secret_id
  kms_key_arn = var.docdb_credentials_kms_key_arn
  role_id     = module.function.role_id
  # Distinct from the default: two instances of this module attach to the
  # same role, and the inline policy name must be unique on that role.
  policy_name = "docdb-credentials"
}

# --- Schedule ------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "${var.name}-schedule"
  description         = "Invokes ${var.name} on a schedule. The event payload is ignored."
  schedule_expression = var.schedule_expression
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "function" {
  rule = aws_cloudwatch_event_rule.schedule.name
  arn  = module.function.function_arn
}

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.function.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.schedule.arn
}
