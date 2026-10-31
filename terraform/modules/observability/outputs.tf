output "kms_key_arn" {
  value = aws_kms_key.this.arn
}

output "gateway_log_group_name" {
  value = aws_cloudwatch_log_group.gateway.name
}

output "gateway_log_group_arn" {
  value = aws_cloudwatch_log_group.gateway.arn
}

output "flow_log_group_arn" {
  value = aws_cloudwatch_log_group.flow.arn
}

output "bedrock_log_group_arn" {
  value = aws_cloudwatch_log_group.bedrock.arn
}

output "apigw_log_group_arn" {
  value = aws_cloudwatch_log_group.apigw.arn
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "dashboard_name" {
  value = aws_cloudwatch_dashboard.this.dashboard_name
}

output "waf_log_group_arn" {
  value = aws_cloudwatch_log_group.waf.arn
}
