variable "secret_id" {
  description = <<-EOT
    Name or ARN of an EXISTING Secrets Manager secret holding the Grafana Cloud
    Cloud Access Policy token.

    This module never creates the secret and never takes the token as an input.
    A token passed through Terraform lands in state in plaintext, in any plan
    output that is shared, and in CI logs on a verbose run. Create it once, out
    of band:

      aws secretsmanager create-secret --name grafana-cloud/loki \
        --secret-string '{"tenant_id":"123456","token":"glc_..."}'

    The secret may be a bare token string or a JSON object; the Python library
    accepts either.
  EOT
  type        = string
}

variable "role_id" {
  description = "IAM role id to grant read access to, normally a lambda-function module's role_id output."
  type        = string
}

variable "kms_key_arn" {
  description = <<-EOT
    KMS key ARN when the secret uses a customer-managed key.

    Reading such a secret needs kms:Decrypt as well as
    secretsmanager:GetSecretValue. Without it the call fails with AccessDenied
    naming Secrets Manager, which sends you looking at the wrong policy. Leave
    null for the default aws/secretsmanager key.
  EOT
  type        = string
  default     = null
}

variable "policy_name" {
  description = "Name of the inline policy attached to the role."
  type        = string
  default     = "grafana-cloud-credentials"
}
