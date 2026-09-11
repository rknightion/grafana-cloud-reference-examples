output "function_name" {
  description = "Name of the deployed function."
  value       = module.function.function_name
}

output "function_arn" {
  description = "ARN of the deployed function."
  value       = module.function.function_arn
}

output "log_group_name" {
  description = "Where the function's own logs go. Not where your AEM logs go."
  value       = module.function.log_group_name
}

output "runtime" {
  description = "Runtime actually configured."
  value       = module.function.runtime
}

output "queue_url" {
  description = "Notification queue URL. Null when use_sqs is false."
  value       = module.event_source.queue_url
}

output "dlq_url" {
  description = "Dead-letter queue URL. Redrive from here after fixing a failure."
  value       = module.event_source.dlq_url
}

output "loki_query" {
  description = "A LogQL query that returns everything this function ships."
  # jsonencode rather than string interpolation, so a service_name containing a
  # quote or a backslash produces a valid selector rather than a broken one.
  value = "{service_name=${jsonencode(var.service_name)}}"
}

output "dashboard_variables" {
  description = <<-EOT
    What to set the shipped dashboards' variables to. They are driven by
    label_values, so a label this deployment leaves empty simply offers no
    choice for that variable.
  EOT
  value = {
    service = var.service_name
    env_id  = var.aem_env_id != "" ? var.aem_env_id : "(not set; the variable will list nothing)"
    tier    = var.aem_tier != "" ? var.aem_tier : "(from the object key)"
  }
}
