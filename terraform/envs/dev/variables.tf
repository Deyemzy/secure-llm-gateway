variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile. null = the standard credential chain (env vars, SSO, instance role)."
  type        = string
  default     = null
}

variable "inference_profile_ids" {
  description = "Allowlisted Bedrock models. Must match model_id values in gateway/policy.yaml."
  type        = list(string)
  default = [
    "us.anthropic.claude-haiku-4-5-20251001-v1:0", # fast
    "us.anthropic.claude-sonnet-5-5",              # smart
    "us.meta.llama4-maverick-17b-instruct-v1:0",   # open
  ]
}

variable "teams" {
  description = "Cognito groups. Must match teams in gateway/policy.yaml."
  type        = list(string)
  default     = ["platform", "research"]
}

variable "revocation_window_minutes" {
  description = <<-EOT
    Upper bound on how long any revocation takes to be fully enforced. Drives BOTH the Cognito access
    token lifetime (a disabled user's last token expires) and POLICY_MAX_STALE_S (a policy change that
    can't be loaded stops being served). See docs/threat-model.md "Revocation".
  EOT
  type        = number
  default     = 15
}

variable "image_tag" {
  description = "Gateway image tag in ECR (immutable). Push with `make push`."
  type        = string
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "enforce_guardrail" {
  type    = bool
  default = true
}

variable "enable_invocation_logging" {
  description = "Account+region-wide Bedrock invocation logging (metadata only)."
  type        = bool
  default     = true
}

variable "hourly_spend_alarm_usd" {
  type    = number
  default = 5
}

variable "alert_email" {
  type    = string
  default = null
}
