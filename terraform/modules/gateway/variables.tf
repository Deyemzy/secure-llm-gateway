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

variable "endpoints_security_group_id" {
  type = string
}

variable "s3_prefix_list_id" {
  type = string
}

variable "alb_security_group_id" {
  type = string
}

variable "target_group_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "task_role_name" {
  type = string
}

variable "environment" {
  description = "ENVIRONMENT for the app. Never \"local\" in AWS."
  type        = string
  validation {
    condition     = var.environment != "local"
    error_message = "local is reserved for developer machines."
  }
}

variable "policy_yaml" {
  description = "Contents of gateway/policy.yaml, published to SSM."
  type        = string
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "redis_replicas" {
  description = <<-EOT
    Replicas for the budget ledger. Default 1 (Multi-AZ, automatic failover): with 0, a node replacement
    comes back EMPTY and every budget resets to zero spent, an under-count. The gateway WAITs for this
    many replica acks before spending, so a failover can't lose an acknowledged hold.
  EOT
  type        = number
  default     = 1
  validation {
    condition     = var.redis_replicas >= 1 || var.allow_unreplicated_ledger
    error_message = "redis_replicas = 0 makes a node loss reset every budget; set allow_unreplicated_ledger = true to accept that."
  }
}

variable "allow_unreplicated_ledger" {
  description = "Explicitly accept a single-node ledger (cost saving for throwaway environments)."
  type        = bool
  default     = false
}

variable "guardrail_id" {
  type = string
}

variable "guardrail_version" {
  type = string
}

variable "kms_key_arn" {
  type = string
}

variable "log_group_name" {
  type = string
}

variable "log_group_arn" {
  type = string
}

variable "image_tag" {
  description = "ECR tag to deploy (make push). Resolved to its digest at plan time; tasks run the digest."
  type        = string
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "jwt_issuer" {
  type = string
}

variable "jwt_client_ids" {
  type = list(string)
}

variable "jwt_max_lifetime_minutes" {
  description = "Must equal the Cognito access token lifetime."
  type        = number
}

variable "jwks_json" {
  description = "Cognito JWKS document (public keys)."
  type        = string
}

variable "cpu" {
  type    = number
  default = 512
}

variable "memory" {
  type    = number
  default = 1024
}

variable "cpu_architecture" {
  type    = string
  default = "ARM64"
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "force_delete_images" {
  type    = bool
  default = false
}

variable "ledger_rebuild_schedule" {
  description = "When the raise-only ledger rebuild runs."
  type        = string
  default     = "rate(1 hour)"
}

variable "policy_max_stale_minutes" {
  description = "How long the gateway keeps enforcing its last valid policy when refreshes fail, before rejecting all requests."
  type        = number
}

variable "enable_ledger_drill" {
  description = "Create the dev-only ledger drill: a Valkey user that can DEL budget counters, its secret, roles and task definition. Refused unless environment is dev or local."
  type        = bool
  default     = false
}
