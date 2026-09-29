output "function_name" {
  description = "Name of the deployed function."
  value       = module.function.function_name
}

output "log_group_name" {
  description = "Where the function's own logs go. Not where your data goes."
  value       = module.function.log_group_name
}

output "attribution_table_name" {
  description = "DynamoDB table holding the client-address-to-user mapping."
  value       = aws_dynamodb_table.attribution.name
}

output "function_security_group_id" {
  description = "Security group attached to the function. Pass this to the DocumentDB cluster's own security group if docdb_security_group_id was not set."
  value       = aws_security_group.function.id
}
