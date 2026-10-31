variable "region" {
  type    = string
  default = "us-east-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile. null = the standard credential chain."
  type        = string
  default     = null
}

variable "monthly_budget_usd" {
  description = "Account-wide monthly cost budget; one email alert when actual spend passes 100%."
  type        = string
  default     = "50"
}

variable "budget_alert_email" {
  description = "Where budget alerts go (set in terraform.tfvars, not committed)."
  type        = string
}
