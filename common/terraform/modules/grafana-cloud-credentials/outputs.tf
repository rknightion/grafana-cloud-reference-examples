output "secret_arn" {
  description = "ARN of the resolved secret."
  value       = data.aws_secretsmanager_secret.this.arn
}

output "secret_name" {
  description = "Name of the resolved secret. Pass this to the function as GRAFANA_CLOUD_CREDENTIALS_SECRET_ID."
  value       = data.aws_secretsmanager_secret.this.name
}
