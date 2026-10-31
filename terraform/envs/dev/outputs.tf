output "api_base_url" {
  description = "Use as the OpenAI SDK base_url"
  value       = module.edge.api_base_url
}

output "user_pool_id" {
  value = module.edge.user_pool_id
}

output "cli_client_id" {
  value = module.edge.cli_client_id
}

output "ecr_repository_url" {
  value = module.gateway.ecr_repository_url
}

output "cluster_name" {
  value = module.gateway.cluster_name
}

output "service_name" {
  value = module.gateway.service_name
}

output "guardrail_id" {
  value = module.bedrock_access.guardrail_id
}

output "dashboard_name" {
  value = module.observability.dashboard_name
}

output "policy_parameter_name" {
  value = module.gateway.policy_parameter_name
}

output "task_role_arn" {
  value = module.bedrock_access.task_role_arn
}

output "guardrail_arn" {
  value = module.bedrock_access.guardrail_arn
}

output "guardrail_version" {
  value = module.bedrock_access.guardrail_version
}

output "bedrock_vpce_id" {
  value = module.network.bedrock_vpce_id
}

output "private_subnet_ids" {
  value = module.network.private_subnet_ids
}

output "task_security_group_id" {
  value = module.gateway.task_security_group_id
}

output "task_definition_family" {
  value = local.name
}
