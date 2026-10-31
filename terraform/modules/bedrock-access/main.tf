# Least-privilege Bedrock access for the gateway task, plus a Bedrock Guardrail
# that IAM forces onto every invocation.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
  region  = data.aws_region.current.region

  # e.g. us.anthropic.claude-sonnet-5-5 -> anthropic.claude-sonnet-5-5
  profile_arns = [for p in var.inference_profile_ids : "arn:aws:bedrock:${local.region}:${local.account}:inference-profile/${p}"]
  model_arns   = [for p in var.inference_profile_ids : "arn:aws:bedrock:*::foundation-model/${join(".", slice(split(".", p), 1, length(split(".", p))))}"]

  guardrail_ref = "${aws_bedrock_guardrail.this.guardrail_arn}:${aws_bedrock_guardrail_version.this.version}"
}

# ---- Guardrail -------------------------------------------------------------

resource "aws_bedrock_guardrail" "this" {
  name                      = var.name
  description               = "Input/output guardrail for the secure LLM gateway"
  blocked_input_messaging   = "Request blocked by gateway guardrail."
  blocked_outputs_messaging = "Response blocked by gateway guardrail."

  content_policy_config {
    filters_config {
      type            = "PROMPT_ATTACK"
      input_strength  = "HIGH"
      output_strength = "NONE"
    }
    dynamic "filters_config" {
      for_each = ["HATE", "INSULTS", "SEXUAL", "VIOLENCE", "MISCONDUCT"]
      content {
        type            = filters_config.value
        input_strength  = "MEDIUM"
        output_strength = "MEDIUM"
      }
    }
  }

  sensitive_information_policy_config {
    dynamic "pii_entities_config" {
      for_each = ["AWS_ACCESS_KEY", "AWS_SECRET_KEY", "PASSWORD"]
      content {
        type   = pii_entities_config.value
        action = "BLOCK"
      }
    }
    dynamic "pii_entities_config" {
      for_each = ["EMAIL", "PHONE", "US_SOCIAL_SECURITY_NUMBER", "CREDIT_DEBIT_CARD_NUMBER", "US_BANK_ACCOUNT_NUMBER"]
      content {
        type   = pii_entities_config.value
        action = "ANONYMIZE"
      }
    }
  }
}

resource "aws_bedrock_guardrail_version" "this" {
  guardrail_arn = aws_bedrock_guardrail.this.guardrail_arn
  description   = "Pinned by Terraform"

  lifecycle {
    replace_triggered_by = [aws_bedrock_guardrail.this]
  }
}

# ---- Task role -------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:ecs:${local.region}:${local.account}:*"]
    }
  }
}

resource "aws_iam_role" "task" {
  name                 = "${var.name}-task"
  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "invoke" {
  # The profile call must arrive through our VPC endpoint.
  statement {
    sid       = "InvokeViaProfileFromVpce"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.profile_arns
    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [var.bedrock_vpce_id]
    }
  }

  # Cross-region routing re-authorises against the underlying model in each region.
  # Only allowed when the call came in through one of our profiles.
  statement {
    sid       = "UnderlyingModelsViaProfileOnly"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = local.model_arns
    condition {
      test     = "StringEquals"
      variable = "bedrock:InferenceProfileArn"
      values   = local.profile_arns
    }
  }

  statement {
    sid       = "UseGuardrail"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [aws_bedrock_guardrail.this.guardrail_arn]
  }

  # No guardrail, no invocation. The gateway sends the guardrail ARN as guardrailIdentifier; both the
  # bare ARN and ARN:version forms of OUR guardrail are accepted, anything else (absent, another
  # guardrail, a bare ID) is denied. scripts/verify_iam.py and scripts/verify_guardrail.sh prove it.
  dynamic "statement" {
    for_each = var.enforce_guardrail ? [1] : []
    content {
      sid       = "DenyInvokeWithoutGuardrail"
      effect    = "Deny"
      actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      resources = ["*"]
      condition {
        test     = "StringNotEquals"
        variable = "bedrock:GuardrailIdentifier"
        values   = [aws_bedrock_guardrail.this.guardrail_arn, local.guardrail_ref]
      }
    }
  }
}

resource "aws_iam_role_policy" "invoke" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.invoke.json
}

# ---- Model invocation logging (account + region wide) ----------------------

data "aws_iam_policy_document" "logging_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "invocation_logging" {
  count              = var.enable_invocation_logging ? 1 : 0
  name               = "${var.name}-bedrock-logging"
  assume_role_policy = data.aws_iam_policy_document.logging_assume.json
}

resource "aws_iam_role_policy" "invocation_logging" {
  count = var.enable_invocation_logging ? 1 : 0
  role  = aws_iam_role.invocation_logging[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${var.invocation_log_group_arn}:*"
    }]
  })
}

resource "aws_bedrock_model_invocation_logging_configuration" "this" {
  count = var.enable_invocation_logging ? 1 : 0

  logging_config {
    # Metadata (caller, model, token counts) always; prompt/completion text only if opted in.
    text_data_delivery_enabled      = var.log_prompt_text
    image_data_delivery_enabled     = false
    embedding_data_delivery_enabled = false
    video_data_delivery_enabled     = false

    cloudwatch_config {
      log_group_name = element(split(":", var.invocation_log_group_arn), 6)
      role_arn       = aws_iam_role.invocation_logging[0].arn
    }
  }

  depends_on = [aws_iam_role_policy.invocation_logging]
}
