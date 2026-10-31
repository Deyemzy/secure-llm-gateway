output "api_base_url" {
  description = "OpenAI SDK base_url"
  value       = aws_api_gateway_stage.v1.invoke_url
}

output "rest_api_name" {
  value = aws_api_gateway_rest_api.this.name
}

output "stage_name" {
  value = aws_api_gateway_stage.v1.stage_name
}

output "web_acl_name" {
  value = aws_wafv2_web_acl.this.name
}

output "user_pool_id" {
  value = aws_cognito_user_pool.this.id
}

output "cli_client_id" {
  value = aws_cognito_user_pool_client.cli.id
}

output "jwt_issuer" {
  value = local.issuer
}

output "alb_security_group_id" {
  value = aws_security_group.alb.id
}

output "alb_arn_suffix" {
  value = aws_lb.this.arn_suffix
}

output "target_group_arn" {
  value = aws_lb_target_group.gateway.arn
}

output "target_group_arn_suffix" {
  value = aws_lb_target_group.gateway.arn_suffix
}

output "access_token_minutes" {
  value = var.access_token_minutes
}
