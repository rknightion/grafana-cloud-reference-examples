output "queue_arn" {
  description = "Main queue ARN. Null when use_sqs is false. Subscribe this yourself when manage_bucket_notification is false."
  value       = var.use_sqs ? aws_sqs_queue.this[0].arn : null
}

output "queue_url" {
  description = "Main queue URL, for the CLI and for redrive."
  value       = var.use_sqs ? aws_sqs_queue.this[0].id : null
}

output "queue_name" {
  description = "Main queue name."
  value       = var.use_sqs ? aws_sqs_queue.this[0].name : null
}

output "dlq_arn" {
  description = "Dead-letter queue ARN."
  value       = var.use_sqs ? aws_sqs_queue.dlq[0].arn : null
}

output "dlq_url" {
  description = "Dead-letter queue URL. Redrive from here after fixing whatever failed."
  value       = var.use_sqs ? aws_sqs_queue.dlq[0].id : null
}

output "dlq_name" {
  description = "Dead-letter queue name."
  value       = var.use_sqs ? aws_sqs_queue.dlq[0].name : null
}

output "source_bucket_arn" {
  description = "ARN of the source bucket, for building the function's s3:GetObject policy."
  value       = local.bucket_arn
}

output "visibility_timeout_seconds" {
  description = "Visibility timeout actually applied, derived from the function timeout."
  value       = var.use_sqs ? local.visibility_timeout : null
}
