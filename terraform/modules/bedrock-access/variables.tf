variable "name" {
  type = string
}

variable "inference_profile_ids" {
  description = "Allowlisted Bedrock inference profiles, e.g. us.anthropic.claude-sonnet-5-5."
  type        = list(string)
}

variable "bedrock_vpce_id" {
  type = string
}

variable "enforce_guardrail" {
  description = "Deny any InvokeModel call that does not carry this module's guardrail."
  type        = bool
  default     = true
}

variable "enable_invocation_logging" {
  description = "Bedrock model invocation logging. Note: this setting is account+region wide."
  type        = bool
  default     = false
}

variable "invocation_log_group_arn" {
  type    = string
  default = null
}

variable "log_prompt_text" {
  type    = bool
  default = false
}
