variable "name" {
  type = string
}

variable "retention_days" {
  type    = number
  default = 30
}

variable "alert_email" {
  type    = string
  default = null
}

variable "alb_arn_suffix" {
  type = string
}

variable "target_group_arn_suffix" {
  type = string
}

variable "api_name" {
  type = string
}

variable "stage_name" {
  type = string
}

variable "web_acl_name" {
  type = string
}

variable "hourly_spend_alarm_usd" {
  type    = number
  default = 5
}
