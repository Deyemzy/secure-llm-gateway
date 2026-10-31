variable "name" {
  type = string
}

variable "cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "az_count" {
  type    = number
  default = 2
}

variable "bedrock_allowed_resource_arns" {
  description = "Inference-profile and foundation-model ARNs the bedrock-runtime endpoint policy permits."
  type        = list(string)
}

variable "flow_log_group_arn" {
  type = string
}
