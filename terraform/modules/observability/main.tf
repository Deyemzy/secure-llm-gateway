# Encrypted log groups, metrics derived from the gateway.s metadata-only request
# audit events, alarms, and a dashboard.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region
  ns      = "SecureLLMGateway/${var.name}"
}

# ---- KMS -------------------------------------------------------------------

data "aws_iam_policy_document" "kms" {
  statement {
    sid       = "AccountAdmin"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:aws:logs:${local.region}:${local.account}:log-group:*"]
    }
  }
  statement {
    sid       = "SNSFromCloudWatchAlarms"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
  }
}

resource "aws_kms_key" "this" {
  description             = "${var.name} logs and alerts"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}

# ---- Log groups ------------------------------------------------------------

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/${var.name}/gateway"
  retention_in_days = var.retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/${var.name}/vpc-flow"
  retention_in_days = var.retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_cloudwatch_log_group" "bedrock" {
  name              = "/${var.name}/bedrock-invocations"
  retention_in_days = var.retention_days
  kms_key_id        = aws_kms_key.this.arn
}


# WAF requires the aws-waf-logs- prefix.
resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${var.name}"
  retention_in_days = var.retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_cloudwatch_log_group" "apigw" {
  name              = "/${var.name}/apigw-access"
  retention_in_days = var.retention_days
  kms_key_id        = aws_kms_key.this.arn
}

# ---- Metrics from the gateway's request logs ----------------------------------

locals {
  filters = {
    Requests         = { pattern = "{ $.event = \"request\" }", value = "1" }
    Allowed          = { pattern = "{ $.event = \"request\" && $.outcome = \"allow\" }", value = "1" }
    Blocked          = { pattern = "{ $.event = \"request\" && $.outcome = \"block\" }", value = "1" }
    Errors           = { pattern = "{ $.event = \"request\" && $.outcome = \"error\" }", value = "1" }
    AuthFailures     = { pattern = "{ $.event = \"request\" && $.code = \"unauthorized\" }", value = "1" }
    BudgetDenials    = { pattern = "{ $.event = \"request\" && $.code = \"budget_exceeded\" }", value = "1" }
    GuardrailBlocks  = { pattern = "{ $.event = \"request\" && $.code = \"blocked_by_guardrail\" }", value = "1" }
    UpstreamThrottle = { pattern = "{ $.event = \"request\" && $.code = \"upstream_throttled\" }", value = "1" }
    DependencyDown   = { pattern = "{ $.event = \"request\" && ($.code = \"dependency_unavailable\" || $.code = \"upstream_unavailable\") }", value = "1" }
    OutputRedactions = { pattern = "{ $.event = \"request\" && $.output_redacted IS TRUE }", value = "1" }
    SpendUSD         = { pattern = "{ $.event = \"request\" && $.cost_usd > 0 }", value = "$.cost_usd" }
    HoldsReleased    = { pattern = "{ $.event = \"reconcile\" && $.outcome = \"expired_released\" }", value = "1" }
    HoldsCharged     = { pattern = "{ $.event = \"reconcile\" && $.outcome = \"expired_charged\" }", value = "1" }
    ReplicaAckFailed = { pattern = "{ $.event = \"request\" && $.detail = \"replica_ack_timeout\" }", value = "1" }
    LedgerRaised     = { pattern = "{ $.event = \"ledger_rebuild_raise\" }", value = "1" }
    LedgerRebuildRun = { pattern = "{ $.event = \"ledger_rebuild\" }", value = "1" }
    PolicyRejected   = { pattern = "{ $.event = \"policy_refresh\" && $.outcome = \"rejected\" }", value = "1" }
    UnknownOutcomes  = { pattern = "{ $.event = \"request\" && $.outcome_unknown IS TRUE }", value = "1" }
    PolicyStale      = { pattern = "{ $.event = \"request\" && $.code = \"policy_unavailable\" }", value = "1" }
  }
}

resource "aws_cloudwatch_log_metric_filter" "gateway" {
  for_each       = local.filters
  name           = "${var.name}-${each.key}"
  log_group_name = aws_cloudwatch_log_group.gateway.name
  pattern        = each.value.pattern
  metric_transformation {
    name      = each.key
    namespace = local.ns
    value     = each.value.value
  }
}

# Blocks broken down by reason code (model_not_allowed, sensitive_data, ...).
resource "aws_cloudwatch_log_metric_filter" "blocked_by_code" {
  name           = "${var.name}-blocked-by-code"
  log_group_name = aws_cloudwatch_log_group.gateway.name
  pattern        = "{ $.event = \"request\" && $.outcome = \"block\" }"
  metric_transformation {
    name       = "BlockedByCode"
    namespace  = local.ns
    value      = "1"
    dimensions = { Code = "$.code" }
  }
}

# Any REJECT on the private subnets means something tried to leave the sandbox.
resource "aws_cloudwatch_log_metric_filter" "egress_reject" {
  name           = "${var.name}-flow-reject"
  log_group_name = aws_cloudwatch_log_group.flow.name
  pattern        = "[version, account, eni, source, destination, srcport, destport, protocol, packets, bytes, windowstart, windowend, action=\"REJECT\", flowlogstatus]"
  metric_transformation {
    name      = "FlowRejects"
    namespace = local.ns
    value     = "1"
  }
}

# ---- Alerts ----------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-alerts"
  kms_master_key_id = aws_kms_key.this.arn
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alert_email == null ? 0 : 1
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alarms = {
    auth-failure-spike = {
      desc = "Invalid tokens reaching the gateway (bypassed API Gateway, or forged tokens)"
      ns   = local.ns, metric = "AuthFailures", stat = "Sum", threshold = 10, period = 300, dims = {}
    }
    block-spike = {
      desc = "Burst of policy blocks (model probing, smuggled fields, credentials in prompts)"
      ns   = local.ns, metric = "Blocked", stat = "Sum", threshold = 50, period = 300, dims = {}
    }
    output-redaction = {
      desc = "Model output contained secrets or PII"
      ns   = local.ns, metric = "OutputRedactions", stat = "Sum", threshold = 1, period = 300, dims = {}
    }
    dependency-down = {
      desc = "Redis or Bedrock unavailable; gateway failing closed"
      ns   = local.ns, metric = "DependencyDown", stat = "Sum", threshold = 5, period = 300, dims = {}
    }
    holds-expired-charged = {
      desc = "A request died mid model call; its worst-case hold was kept. Reconcile against Bedrock invocation logs (requestMetadata.ledger_id)"
      ns   = local.ns, metric = "HoldsCharged", stat = "Sum", threshold = 1, period = 300, dims = {}
    }
    replica-ack-failures = {
      desc = "Budget holds not acknowledged by the Valkey replica within the WAIT timeout; requests failing closed (503). Check replica health / replication lag"
      ns   = local.ns, metric = "ReplicaAckFailed", stat = "Sum", threshold = 1, period = 60, dims = {}
    }
    ledger-lost-spend = {
      desc = "The hourly rebuild found the budget ledger LOWER than logged spend and raised it (Valkey lost data). Investigate the cache."
      ns   = local.ns, metric = "LedgerRaised", stat = "Sum", threshold = 1, period = 3600, dims = {}
    }
    ledger-rebuild-missing = {
      desc = "The hourly ledger rebuild has not reported in 2 hours"
      ns   = local.ns, metric = "LedgerRebuildRun", stat = "Sum", threshold = 1, period = 7200, dims = {}
    }
    policy-refresh-rejected = {
      desc = "A policy update failed validation; gateway still enforcing the last valid policy (503s begin after POLICY_MAX_STALE_S)"
      ns   = local.ns, metric = "PolicyRejected", stat = "Sum", threshold = 1, period = 60, dims = {}
    }
    policy-stale = {
      desc = "No valid policy refresh within max staleness; gateway rejecting all requests"
      ns   = local.ns, metric = "PolicyStale", stat = "Sum", threshold = 1, period = 60, dims = {}
    }
    hourly-spend = {
      desc = "Bedrock spend through the gateway above threshold in the last hour"
      ns   = local.ns, metric = "SpendUSD", stat = "Sum", threshold = var.hourly_spend_alarm_usd, period = 3600, dims = {}
    }
    apigw-5xx = {
      desc = "API Gateway returning 5xx"
      ns   = "AWS/ApiGateway", metric = "5XXError", stat = "Sum", threshold = 10, period = 300
      dims = { ApiName = var.api_name, Stage = var.stage_name }
    }
    waf-blocks = {
      desc = "WAF blocking a burst of requests"
      ns   = "AWS/WAFV2", metric = "BlockedRequests", stat = "Sum", threshold = 100, period = 300
      dims = { WebACL = var.web_acl_name, Region = local.region, Rule = "ALL" }
    }
    unhealthy-targets = {
      desc = "No healthy gateway tasks"
      ns   = "AWS/ApplicationELB", metric = "HealthyHostCount", stat = "Minimum", threshold = 1, period = 300
      dims = { LoadBalancer = var.alb_arn_suffix, TargetGroup = var.target_group_arn_suffix }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.desc
  namespace           = each.value.ns
  metric_name         = each.value.metric
  statistic           = each.value.stat
  dimensions          = each.value.dims
  period              = each.value.period
  evaluation_periods  = 1
  threshold           = each.value.threshold
  comparison_operator = contains(["unhealthy-targets", "ledger-rebuild-missing"], each.key) ? "LessThanThreshold" : "GreaterThanOrEqualToThreshold"
  treat_missing_data  = contains(["unhealthy-targets", "ledger-rebuild-missing"], each.key) ? "breaching" : "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# ---- Dashboard -------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = var.name
  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title   = "Outcomes", region = local.region, stat = "Sum", period = 300, view = "timeSeries"
          metrics = [[local.ns, "Allowed"], [local.ns, "Blocked"], [local.ns, "Errors"], [local.ns, "AuthFailures"]]
        }
      },
      {
        type = "log", x = 12, y = 0, width = 12, height = 6
        properties = {
          title = "Blocks and errors by code", region = local.region, view = "bar"
          query = "SOURCE '${aws_cloudwatch_log_group.gateway.name}' | filter event = 'request' and outcome != 'allow' | stats count(*) as n by code | sort n desc"
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title   = "Spend (USD)", region = local.region, stat = "Sum", period = 3600, view = "timeSeries"
          metrics = [[local.ns, "SpendUSD"]]
        }
      },
      {
        type = "log", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Spend by team and model", region = local.region, view = "table"
          query = "SOURCE '${aws_cloudwatch_log_group.gateway.name}' | filter event = 'request' and cost_usd > 0 | stats sum(cost_usd) as usd, count(*) as calls by team, model | sort usd desc"
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title   = "Upstream and budget", region = local.region, stat = "Sum", period = 300, view = "timeSeries"
          metrics = [[local.ns, "GuardrailBlocks"], [local.ns, "UpstreamThrottle"], [local.ns, "DependencyDown"], [local.ns, "BudgetDenials"]]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title = "Edge and egress", region = local.region, stat = "Sum", period = 300, view = "timeSeries"
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiName", var.api_name, "Stage", var.stage_name],
            ["AWS/ApiGateway", "4XXError", "ApiName", var.api_name, "Stage", var.stage_name],
            ["AWS/ApiGateway", "5XXError", "ApiName", var.api_name, "Stage", var.stage_name],
            ["AWS/WAFV2", "BlockedRequests", "WebACL", var.web_acl_name, "Region", local.region, "Rule", "ALL"],
            [local.ns, "FlowRejects"],
          ]
        }
      },
    ]
  })
}
