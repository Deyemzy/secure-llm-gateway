variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "teams" {
  description = "Cognito groups = gateway teams. Must match teams in gateway/policy.yaml."
  type        = list(string)
}

variable "target_port" {
  type    = number
  default = 8080
}

variable "throttle_rate" {
  description = "Stage-wide steady-state requests/second (per-user limits are enforced in Redis)."
  type        = number
  default     = 20
}

variable "rate_limit_per_ip" {
  description = "WAF: requests per IP per 5 minutes."
  type        = number
  default     = 600
}

variable "rate_limit_per_token" {
  description = "WAF: requests per Authorization header per 5 minutes."
  type        = number
  default     = 300
}

variable "waf_log_group_arn" {
  type = string
}

variable "manage_apigw_account_logging_role" {
  description = "Set the account-wide API Gateway CloudWatch role (one per account+region). Disable if another stack owns it."
  type        = bool
  default     = true
}

variable "throttle_burst" {
  type    = number
  default = 40
}

variable "access_log_group_arn" {
  type = string
}

variable "deletion_protection" {
  type    = bool
  default = false
}

variable "force_destroy_logs" {
  type    = bool
  default = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "access_token_minutes" {
  description = "Cognito access/ID token lifetime. The gateway rejects tokens living longer than this."
  type        = number
  default     = 15
  validation {
    condition     = var.access_token_minutes >= 5 && var.access_token_minutes <= 60
    error_message = "Use 5-60 minutes."
  }
}
