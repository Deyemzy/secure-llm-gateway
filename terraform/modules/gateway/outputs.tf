output "ecr_repository_url" {
  value = aws_ecr_repository.gateway.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "service_name" {
  value = aws_ecs_service.gateway.name
}

output "redis_endpoint" {
  value = aws_elasticache_replication_group.budget.primary_endpoint_address
}

output "task_security_group_id" {
  value = aws_security_group.task.id
}

output "policy_parameter_name" {
  value = aws_ssm_parameter.policy.name
}
