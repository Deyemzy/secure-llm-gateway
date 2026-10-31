# Front door: WAF -> API Gateway REST API (Cognito user pool authorizer) -> VPC Link v2 -> internal ALB.
# Only two methods exist; anything else is a 404 at API Gateway.
# The gateway re-verifies the token itself, so reaching the ALB from inside the VPC is not enough.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_elb_service_account" "this" {}

locals {
  # Stage "v1" + these resources => https://<api-id>.execute-api.<region>.amazonaws.com/v1/chat/completions,
  # so OpenAI SDKs use base_url ".../v1" unchanged.
  stage   = "v1"
  backend = { chat = "/v1/chat/completions", models = "/v1/models" }
  issuer  = "https://${aws_cognito_user_pool.this.endpoint}"
  # Cognito access tokens from SRP/password sign-in carry this scope. Requiring a scope makes the
  # authorizer accept access tokens (not ID tokens), matching the gateway's own token_use check.
  scopes = ["aws.cognito.signin.user.admin"]
}

# ---- Identity: Cognito -----------------------------------------------------

resource "aws_cognito_user_pool" "this" {
  name                = var.name
  deletion_protection = var.deletion_protection ? "ACTIVE" : "INACTIVE"
  mfa_configuration   = "OPTIONAL"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  username_configuration {
    case_sensitive = false
  }
}

resource "aws_cognito_user_pool_client" "cli" {
  name                          = "${var.name}-cli"
  user_pool_id                  = aws_cognito_user_pool.this.id
  generate_secret               = false
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]

  # Short-lived access tokens limit what a stolen token is worth; clients renew silently
  # with the refresh token, and disabling a user takes effect at the next refresh.
  access_token_validity  = var.access_token_minutes
  id_token_validity      = var.access_token_minutes
  refresh_token_validity = 12

  # Users may edit only their display name through this client. Team membership lives in
  # Cognito groups (admin-only) and the pool defines no custom attributes.
  read_attributes  = ["name", "email"]
  write_attributes = ["name"]
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}

# Team = Cognito group; the gateway requires exactly one per user.
resource "aws_cognito_user_group" "team" {
  for_each     = toset(var.teams)
  name         = each.value
  user_pool_id = aws_cognito_user_pool.this.id
}

# ---- Internal ALB ----------------------------------------------------------

resource "aws_security_group" "vpc_link" {
  name        = "${var.name}-vpclink"
  description = "API Gateway VPC link ENIs"
  vpc_id      = var.vpc_id
}

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Internal ALB: ingress from the API Gateway VPC link only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "vpc_link_to_alb" {
  security_group_id            = aws_security_group.vpc_link.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_vpc_link" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.vpc_link.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_targets" {
  security_group_id = aws_security_group.alb.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = var.target_port
  to_port           = var.target_port
}

resource "aws_lb" "this" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = true
  subnets                    = var.private_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
  enable_deletion_protection = var.deletion_protection
  idle_timeout               = 60

  access_logs {
    bucket  = aws_s3_bucket.logs.id
    prefix  = "alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.logs]
}

resource "aws_lb_target_group" "gateway" {
  name                 = var.name
  port                 = var.target_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = var.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "application/json"
      status_code  = "404"
      message_body = "{\"error\":{\"code\":\"not_found\",\"message\":\"not found\"}}"
    }
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = values(local.backend)
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gateway.arn
  }
}

# ---- API Gateway REST API --------------------------------------------------

resource "aws_apigatewayv2_vpc_link" "this" {
  name               = var.name
  subnet_ids         = var.private_subnet_ids
  security_group_ids = [aws_security_group.vpc_link.id]
}

resource "aws_api_gateway_rest_api" "this" {
  name        = var.name
  description = "Secure LLM gateway (OpenAI-compatible subset)"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_authorizer" "cognito" {
  name            = "cognito"
  rest_api_id     = aws_api_gateway_rest_api.this.id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = [aws_cognito_user_pool.this.arn]
  identity_source = "method.request.header.Authorization"
}

resource "aws_api_gateway_resource" "chat" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "chat"
}

resource "aws_api_gateway_resource" "completions" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_resource.chat.id
  path_part   = "completions"
}

resource "aws_api_gateway_resource" "models" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "models"
}

locals {
  methods = {
    chat   = { resource_id = aws_api_gateway_resource.completions.id, method = "POST" }
    models = { resource_id = aws_api_gateway_resource.models.id, method = "GET" }
  }
}

resource "aws_api_gateway_method" "this" {
  for_each             = local.methods
  rest_api_id          = aws_api_gateway_rest_api.this.id
  resource_id          = each.value.resource_id
  http_method          = each.value.method
  authorization        = "COGNITO_USER_POOLS"
  authorizer_id        = aws_api_gateway_authorizer.cognito.id
  authorization_scopes = local.scopes
}

resource "aws_api_gateway_integration" "this" {
  for_each                = local.methods
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = each.value.resource_id
  http_method             = aws_api_gateway_method.this[each.key].http_method
  type                    = "HTTP_PROXY"
  integration_http_method = each.value.method
  uri                     = "http://${aws_lb.this.dns_name}${local.backend[each.key]}"
  connection_type         = "VPC_LINK"
  connection_id           = aws_apigatewayv2_vpc_link.this.id
  integration_target      = aws_lb.this.arn
  timeout_milliseconds    = 29000

  # Log-only: the caller's real IP, overwriting anything the client sent.
  request_parameters = {
    "integration.request.header.x-client-ip" = "context.identity.sourceIp"
  }
}

# OpenAI-shaped errors for everything API Gateway rejects itself, so SDKs surface them cleanly.
locals {
  gateway_errors = {
    UNAUTHORIZED                 = { status = 401, type = "authentication_error", code = "unauthorized", msg = "missing or invalid token", retry = "false" }
    ACCESS_DENIED                = { status = 403, type = "permission_error", code = "forbidden", msg = "access denied", retry = "false" }
    MISSING_AUTHENTICATION_TOKEN = { status = 404, type = "not_found_error", code = "not_found", msg = "not found", retry = "false" }
    WAF_FILTERED                 = { status = 403, type = "permission_error", code = "blocked_by_firewall", msg = "request blocked", retry = "false" }
    THROTTLED                    = { status = 429, type = "rate_limit_error", code = "rate_limited", msg = "too many requests", retry = "true" }
    REQUEST_TOO_LARGE            = { status = 413, type = "invalid_request_error", code = "request_too_large", msg = "request too large", retry = "false" }
    INTEGRATION_TIMEOUT          = { status = 504, type = "api_error", code = "timeout", msg = "upstream timed out; lower max_tokens", retry = "false" }
    DEFAULT_5XX                  = { status = null, type = "api_error", code = "gateway_error", msg = "gateway error", retry = "true" }
  }
}

resource "aws_api_gateway_gateway_response" "this" {
  for_each      = local.gateway_errors
  rest_api_id   = aws_api_gateway_rest_api.this.id
  response_type = each.key
  status_code   = each.value.status == null ? null : tostring(each.value.status)

  response_templates = {
    "application/json" = jsonencode({ error = { message = each.value.msg, type = each.value.type, code = each.value.code, param = null } })
  }
  response_parameters = {
    "gatewayresponse.header.x-request-id"   = "context.requestId"
    "gatewayresponse.header.x-should-retry" = "'${each.value.retry}'"
  }
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeploy = sha1(jsonencode([
      aws_api_gateway_resource.chat, aws_api_gateway_resource.completions, aws_api_gateway_resource.models,
      aws_api_gateway_method.this, aws_api_gateway_integration.this, aws_api_gateway_authorizer.cognito,
      aws_api_gateway_gateway_response.this,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Access logging needs an account-level CloudWatch role for API Gateway (one per account+region).
data "aws_iam_policy_document" "apigw_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "apigw_logs" {
  count              = var.manage_apigw_account_logging_role ? 1 : 0
  name               = "${var.name}-apigw-logs"
  assume_role_policy = data.aws_iam_policy_document.apigw_assume.json
}

resource "aws_iam_role_policy_attachment" "apigw_logs" {
  count      = var.manage_apigw_account_logging_role ? 1 : 0
  role       = aws_iam_role.apigw_logs[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

resource "aws_api_gateway_account" "this" {
  count               = var.manage_apigw_account_logging_role ? 1 : 0
  cloudwatch_role_arn = aws_iam_role.apigw_logs[0].arn
  depends_on          = [aws_iam_role_policy_attachment.apigw_logs]
}

resource "aws_api_gateway_stage" "v1" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = local.stage

  access_log_settings {
    destination_arn = var.access_log_group_arn
    format = jsonencode({
      requestId    = "$context.requestId"
      ip           = "$context.identity.sourceIp"
      requestTime  = "$context.requestTimeEpoch"
      method       = "$context.httpMethod"
      path         = "$context.resourcePath"
      status       = "$context.status"
      responseLen  = "$context.responseLength"
      latencyMs    = "$context.responseLatency"
      user         = "$context.authorizer.claims.username"
      authError    = "$context.authorizer.error"
      wafStatus    = "$context.waf.status"
      integration  = "$context.integration.status"
      integrationE = "$context.integration.error"
    })
  }

  depends_on = [aws_api_gateway_account.this]
}

resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.v1.stage_name
  method_path = "*/*"

  settings {
    metrics_enabled        = true
    logging_level          = "ERROR"
    data_trace_enabled     = false # never log request/response bodies
    throttling_rate_limit  = var.throttle_rate
    throttling_burst_limit = var.throttle_burst
  }
}

# ---- WAF on the stage ------------------------------------------------------

resource "aws_wafv2_web_acl" "this" {
  name  = var.name
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  # API Gateway's WAF inspects only the first part of the body, so size is judged by Content-Length.
  rule {
    name     = "block-large-bodies"
    priority = 0
    action {
      block {}
    }
    statement {
      regex_match_statement {
        regex_string = "^([3-9][0-9]{5}|[0-9]{7,})$" # >= 300,000 bytes
        field_to_match {
          single_header {
            name = "content-length"
          }
        }
        text_transformation {
          priority = 0
          type     = "NONE"
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-large-body"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-per-ip"
    priority = 1
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit                 = var.rate_limit_per_ip
        evaluation_window_sec = 300
        aggregate_key_type    = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-ip"
      sampled_requests_enabled   = true
    }
  }

  # Per-token flood control at the edge (budgets and per-user rpm are enforced in the gateway).
  rule {
    name     = "rate-per-token"
    priority = 2
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit                 = var.rate_limit_per_token
        evaluation_window_sec = 300
        aggregate_key_type    = "CUSTOM_KEYS"
        custom_key {
          header {
            name = "authorization"
            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-token"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-ip-reputation"
    priority = 10
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 11
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # Prompts are large and often contain code/URLs, so body-content rules only count;
  # body policy is enforced by the gateway's strict schema and the Guardrail.
  rule {
    name     = "aws-common"
    priority = 12
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
        dynamic "rule_action_override" {
          for_each = ["SizeRestrictions_BODY", "CrossSiteScripting_BODY", "GenericLFI_BODY", "GenericRFI_BODY", "EC2MetaDataSSRF_BODY"]
          content {
            name = rule_action_override.value
            action_to_use {
              count {}
            }
          }
        }
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-common"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "stage" {
  resource_arn = aws_api_gateway_stage.v1.arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [var.waf_log_group_arn]

  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
}

# ---- ALB access logs -------------------------------------------------------

resource "aws_s3_bucket" "logs" {
  bucket_prefix = "${var.name}-alb-logs-"
  force_destroy = var.force_destroy_logs
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ALB access logs only support SSE-S3.
resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = var.log_retention_days
    }
  }
}

data "aws_iam_policy_document" "logs" {
  statement {
    sid       = "ELBWrite"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.logs.arn}/alb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_elb_service_account.this.arn]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "logs" {
  bucket     = aws_s3_bucket.logs.id
  policy     = data.aws_iam_policy_document.logs.json
  depends_on = [aws_s3_bucket_public_access_block.logs]
}
