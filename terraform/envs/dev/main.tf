data "aws_caller_identity" "current" {}

locals {
  name    = "slg-dev"
  account = data.aws_caller_identity.current.account_id

  # Must line up with the models in gateway/policy.yaml.
  profile_arns = [for p in var.inference_profile_ids : "arn:aws:bedrock:${var.region}:${local.account}:inference-profile/${p}"]
  model_arns   = [for p in var.inference_profile_ids : "arn:aws:bedrock:*::foundation-model/${join(".", slice(split(".", p), 1, length(split(".", p))))}"]

  # gateway/policy.yaml plus the dev-only live-test team and users. Published as JSON (valid YAML).
  base_policy = yamldecode(file("${path.root}/../../../gateway/policy.yaml"))
  live_test   = yamldecode(file("${path.root}/live-test-policy.yaml"))
  policy = merge(local.base_policy, {
    teams = merge(local.base_policy.teams, local.live_test.teams)
    budgets = merge(local.base_policy.budgets, {
      teams = merge(local.base_policy.budgets.teams, local.live_test.budgets.teams)
      users = merge(local.base_policy.budgets.users, local.live_test.budgets.users)
    })
  })
}

module "observability" {
  source = "../../modules/observability"

  name                    = local.name
  alert_email             = var.alert_email
  api_name                = module.edge.rest_api_name
  stage_name              = module.edge.stage_name
  web_acl_name            = module.edge.web_acl_name
  alb_arn_suffix          = module.edge.alb_arn_suffix
  target_group_arn_suffix = module.edge.target_group_arn_suffix
  hourly_spend_alarm_usd  = var.hourly_spend_alarm_usd
}

module "network" {
  source = "../../modules/network"

  name                          = local.name
  bedrock_allowed_resource_arns = concat(local.profile_arns, local.model_arns)
  flow_log_group_arn            = module.observability.flow_log_group_arn
}

module "bedrock_access" {
  source = "../../modules/bedrock-access"

  name                      = local.name
  inference_profile_ids     = var.inference_profile_ids
  bedrock_vpce_id           = module.network.bedrock_vpce_id
  enforce_guardrail         = var.enforce_guardrail
  enable_invocation_logging = var.enable_invocation_logging
  invocation_log_group_arn  = module.observability.bedrock_log_group_arn
}

module "edge" {
  source = "../../modules/edge"

  name                 = local.name
  vpc_id               = module.network.vpc_id
  vpc_cidr             = module.network.vpc_cidr
  private_subnet_ids   = module.network.private_subnet_ids
  teams                = distinct(concat(var.teams, keys(local.live_test.teams)))
  access_log_group_arn = module.observability.apigw_log_group_arn
  access_token_minutes = var.revocation_window_minutes
  waf_log_group_arn    = module.observability.waf_log_group_arn
  force_destroy_logs   = true
}

# Cognito's public signing keys, fetched at deploy time and baked into the task definition
# (the tasks have no internet route). Re-apply if Cognito ever rotates them.
data "http" "jwks" {
  url = "${module.edge.jwt_issuer}/.well-known/jwks.json"

  lifecycle {
    postcondition {
      condition     = self.status_code == 200 && can(jsondecode(self.response_body).keys[0].kid)
      error_message = "Could not fetch the Cognito JWKS."
    }
  }
}

module "gateway" {
  source = "../../modules/gateway"

  name                        = local.name
  vpc_id                      = module.network.vpc_id
  vpc_cidr                    = module.network.vpc_cidr
  private_subnet_ids          = module.network.private_subnet_ids
  endpoints_security_group_id = module.network.endpoints_security_group_id
  s3_prefix_list_id           = module.network.s3_prefix_list_id
  alb_security_group_id       = module.edge.alb_security_group_id
  target_group_arn            = module.edge.target_group_arn
  task_role_arn               = module.bedrock_access.task_role_arn
  task_role_name              = module.bedrock_access.task_role_name
  environment                 = "dev"
  policy_yaml                 = jsonencode(local.policy)
  guardrail_id                = module.bedrock_access.guardrail_arn # ARN: matched by the IAM deny condition
  guardrail_version           = module.bedrock_access.guardrail_version
  kms_key_arn                 = module.observability.kms_key_arn
  log_group_name              = module.observability.gateway_log_group_name
  log_group_arn               = module.observability.gateway_log_group_arn
  jwt_issuer                  = module.edge.jwt_issuer
  jwt_client_ids              = [module.edge.cli_client_id]
  jwt_max_lifetime_minutes    = module.edge.access_token_minutes
  policy_max_stale_minutes    = var.revocation_window_minutes
  jwks_json                   = jsonencode(jsondecode(data.http.jwks.response_body))
  image_tag                   = var.image_tag
  desired_count               = var.desired_count
  force_delete_images         = true
  enable_ledger_drill         = true # make drill-ledger-rebuild (dev only)
}
