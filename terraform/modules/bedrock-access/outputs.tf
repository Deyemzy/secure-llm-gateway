output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "task_role_name" {
  value = aws_iam_role.task.name
}

output "guardrail_id" {
  value = aws_bedrock_guardrail.this.guardrail_id
}

output "guardrail_arn" {
  value = aws_bedrock_guardrail.this.guardrail_arn
}

output "guardrail_version" {
  value = aws_bedrock_guardrail_version.this.version
}

output "decoy_guardrail_arn" {
  value = aws_bedrock_guardrail.decoy.guardrail_arn
}

output "decoy_guardrail_version" {
  value = aws_bedrock_guardrail_version.decoy.version
}
