# Grants a role read access to an existing Grafana Cloud credential secret.
#
# Read-only and create-nothing by design: the secret is the one resource in this
# repo that must never be managed by the IaC, because managing it means the token
# passes through Terraform state.

data "aws_secretsmanager_secret" "this" {
  # Resolves a bare name or a full ARN, so a caller can pass whichever they have.
  name = var.secret_id
}

data "aws_iam_policy_document" "read" {
  statement {
    sid       = "ReadGrafanaCloudCredential"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [data.aws_secretsmanager_secret.this.arn]
  }

  dynamic "statement" {
    for_each = var.kms_key_arn == null ? [] : [var.kms_key_arn]

    content {
      sid       = "DecryptCredentialWithCustomerManagedKey"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${data.aws_region.current.region}.amazonaws.com"]
      }

      # Secrets Manager sets SecretARN in the encryption context on every
      # decrypt. Without this condition the grant covers every secret encrypted
      # with the same key, which on a shared key is every secret in the account.
      condition {
        test     = "StringEquals"
        variable = "kms:EncryptionContext:SecretARN"
        values   = [data.aws_secretsmanager_secret.this.arn]
      }
    }
  }
}

data "aws_region" "current" {}

resource "aws_iam_role_policy" "read" {
  name   = var.policy_name
  role   = var.role_id
  policy = data.aws_iam_policy_document.read.json
}
