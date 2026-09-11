output "function_name" {
  description = "Name of the created function."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "ARN of the created function."
  value       = aws_lambda_function.this.arn
}

output "function_qualified_arn" {
  description = <<-EOT
    Version-qualified ARN. Only immutable when `publish` is true; otherwise it
    resolves to $LATEST, which changes on every deploy, so an alias or event
    source mapping pointing at it is not actually pinned to anything.
  EOT
  value       = aws_lambda_function.this.qualified_arn
}

output "function_version" {
  description = "Published version number, or $LATEST when publish is false."
  value       = aws_lambda_function.this.version
}

output "role_name" {
  description = "Execution role name, for attaching a further policy from the caller."
  value       = aws_iam_role.this.name
}

output "role_arn" {
  description = "Execution role ARN."
  value       = aws_iam_role.this.arn
}

output "role_id" {
  description = "Execution role id, for an aws_iam_role_policy in the caller."
  value       = aws_iam_role.this.id
}

output "log_group_name" {
  description = "CloudWatch log group holding the function's own output."
  value       = aws_cloudwatch_log_group.this.name
}

output "log_group_arn" {
  description = "CloudWatch log group ARN."
  value       = aws_cloudwatch_log_group.this.arn
}

output "runtime" {
  description = "Runtime actually configured, after validation."
  value       = aws_lambda_function.this.runtime
}

output "is_preview_runtime" {
  description = "True when the configured runtime is a public-preview one."
  value       = local.is_preview
}
