# FastAPI gateway on Fargate in private subnets, plus the Valkey budget store.
# No public IP, no internet route, read-only root filesystem, non-root, all capabilities dropped.

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region
  image  = "${aws_ecr_repository.gateway.repository_url}:${var.image_tag}"

  # Exactly what reserve.lua / settle.lua and redis-py need, on slg:* keys only.
  # Mirrored in deploy/valkey/users.acl (CI runs the budget tests as this user; a test checks they match).
  redis_acl = join(" ", [
    "on ~slg:* resetchannels -@all",
    "+get +set +del +incr +incrby +expire",
    "+hget +hmget +hset +zadd +zrem +zrangebyscore",
    "+eval +evalsha +script|load +script|exists +wait",
    "+ping +info +client|setinfo",
  ])
}

# ---- Image registry --------------------------------------------------------

resource "aws_ecr_repository" "gateway" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = var.force_delete_images

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
}

resource "aws_ecr_lifecycle_policy" "gateway" {
  repository = aws_ecr_repository.gateway.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# ---- Budget store: Valkey (cluster mode disabled), TLS + ACL users --------

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis"
  description = "Valkey: ingress from gateway tasks only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_tasks" {
  security_group_id            = aws_security_group.redis.id
  referenced_security_group_id = aws_security_group.task.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "random_password" "redis_app" {
  length  = 40
  special = false
}

resource "random_password" "redis_default" {
  length  = 40
  special = false
}

# ElastiCache requires a user named "default" in every group; disable it.
resource "aws_elasticache_user" "default" {
  user_id       = "${var.name}-default"
  user_name     = "default"
  engine        = "valkey"
  access_string = "off -@all"
  authentication_mode {
    type      = "password"
    passwords = [random_password.redis_default.result]
  }
}

resource "aws_elasticache_user" "app" {
  user_id       = "${var.name}-app"
  user_name     = "slg-gateway"
  engine        = "valkey"
  access_string = local.redis_acl
  authentication_mode {
    type      = "password"
    passwords = [random_password.redis_app.result]
  }
}

resource "aws_elasticache_user_group" "this" {
  user_group_id = var.name
  engine        = "valkey"
  user_ids      = [aws_elasticache_user.default.user_id, aws_elasticache_user.app.user_id]
}

resource "aws_elasticache_subnet_group" "budget" {
  name       = var.name
  subnet_ids = var.private_subnet_ids
}

# Cluster mode DISABLED on purpose: reserve.lua touches the team and user budget keys in one
# atomic script, and slg:budget:team:* / slg:budget:user:* would land on different slots in
# cluster mode (CROSSSLOT). One primary (+ optional replica) keeps the documented key format.
resource "aws_elasticache_replication_group" "budget" {
  replication_group_id       = var.name
  description                = "Gateway budget ledger"
  engine                     = "valkey"
  engine_version             = "8.1"
  node_type                  = var.redis_node_type
  num_cache_clusters         = var.redis_replicas + 1
  automatic_failover_enabled = var.redis_replicas > 0
  multi_az_enabled           = var.redis_replicas > 0
  cluster_mode               = "disabled"
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.budget.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  user_group_ids             = [aws_elasticache_user_group.this.user_group_id]
  snapshot_retention_limit   = 3
  apply_immediately          = true
}

resource "aws_secretsmanager_secret" "redis" {
  name_prefix             = "${var.name}/redis-password-"
  kms_key_id              = var.kms_key_arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id     = aws_secretsmanager_secret.redis.id
  secret_string = random_password.redis_app.result
}

# ---- Policy in SSM Parameter Store -----------------------------------------
# gateway/policy.yaml in git is the source of truth; the gateway reads it from here, validates it,
# and refuses to start if it is missing or invalid.

resource "aws_ssm_parameter" "policy" {
  name        = "/${var.name}/policy"
  description = "Gateway policy (models, prices, teams, monthly budgets)"
  type        = "String"
  tier        = length(var.policy_yaml) > 4096 ? "Advanced" : "Standard"
  value       = var.policy_yaml
}

data "aws_iam_policy_document" "task_policy_read" {
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.policy.arn]
  }
  # Ledger rebuild job (python -m app.rebuild): read the month's spend from the audit log group.
  statement {
    actions   = ["logs:StartQuery"]
    resources = ["${var.log_group_arn}:*", var.log_group_arn]
  }
  statement {
    actions   = ["logs:GetQueryResults", "logs:StopQuery"]
    resources = ["*"] # query-id scoped APIs; no resource-level permissions
  }
}

resource "aws_iam_role_policy" "task_policy_read" {
  name   = "gateway-policy-and-ledger-rebuild"
  role   = var.task_role_name
  policy = data.aws_iam_policy_document.task_policy_read.json
}

# ---- IAM: execution role (ECS agent) ---------------------------------------

data "aws_iam_policy_document" "exec_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.exec_assume.json
}

data "aws_iam_policy_document" "execution" {
  statement {
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    actions   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
    resources = [aws_ecr_repository.gateway.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${var.log_group_arn}:*"]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.redis.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution.json
}

# ---- Network ---------------------------------------------------------------

resource "aws_security_group" "task" {
  name        = "${var.name}-task"
  description = "Gateway tasks: ingress from ALB; egress to VPC endpoints, S3 prefix list, Valkey, DNS"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "from_alb" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = var.alb_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = var.container_port
  to_port                      = var.container_port
}

resource "aws_vpc_security_group_egress_rule" "to_endpoints" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = var.endpoints_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "to_s3" {
  security_group_id = aws_security_group.task.id
  description       = "ECR image layers via S3 gateway endpoint"
  prefix_list_id    = var.s3_prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "to_redis" {
  security_group_id            = aws_security_group.task.id
  referenced_security_group_id = aws_security_group.redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# Route 53 Resolver (VPC base + 2) for endpoint private DNS.
resource "aws_vpc_security_group_egress_rule" "dns" {
  for_each          = toset(["udp", "tcp"])
  security_group_id = aws_security_group.task.id
  cidr_ipv4         = "${cidrhost(var.vpc_cidr, 2)}/32"
  ip_protocol       = each.value
  from_port         = 53
  to_port           = 53
}

# ---- ECS -------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = var.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  volume {
    name                = "tmp"
    configure_at_launch = false
  }

  container_definitions = jsonencode([{
    name                   = "gateway"
    image                  = local.image
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    # Defaults ECS fills in on save are written out explicitly, so plans stay clean.
    portMappings    = [{ containerPort = var.container_port, hostPort = var.container_port, protocol = "tcp" }]
    mountPoints     = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    volumesFrom     = []
    systemControls  = []
    linuxParameters = { capabilities = { add = [], drop = ["ALL"] }, initProcessEnabled = true }

    environment = [
      { name = "ENVIRONMENT", value = var.environment },
      { name = "AWS_REGION", value = local.region },
      { name = "POLICY_SSM_PARAMETER", value = aws_ssm_parameter.policy.name },
      { name = "BEDROCK_GUARDRAIL_ID", value = var.guardrail_id },
      { name = "BEDROCK_GUARDRAIL_VERSION", value = var.guardrail_version },
      { name = "REDIS_MIN_REPLICA_ACKS", value = tostring(var.redis_replicas) },
      { name = "GATEWAY_LOG_GROUP", value = var.log_group_name },
      { name = "POLICY_MAX_STALE_S", value = tostring(var.policy_max_stale_minutes * 60) },
      { name = "JWT_ISSUER", value = var.jwt_issuer },
      { name = "JWT_CLIENT_IDS", value = join(",", var.jwt_client_ids) },
      { name = "JWT_MAX_LIFETIME_S", value = tostring(var.jwt_max_lifetime_minutes * 60) },
      # Cognito signing keys, fetched at deploy time (tasks have no route to the internet).
      { name = "JWKS_JSON", value = var.jwks_json },
      { name = "REDIS_HOST", value = aws_elasticache_replication_group.budget.primary_endpoint_address },
      { name = "REDIS_PORT", value = "6379" },
      { name = "REDIS_USER", value = aws_elasticache_user.app.user_name },
      { name = "REDIS_TLS", value = "true" },
    ]
    secrets = [
      { name = "REDIS_PASSWORD", valueFrom = aws_secretsmanager_secret.redis.arn },
    ]

    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:${var.container_port}/healthz',timeout=4).status==200 else 1)"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = var.log_group_name
        awslogs-region        = local.region
        awslogs-stream-prefix = "gateway"
      }
    }
  }])
}

resource "aws_ecs_service" "gateway" {
  name                   = var.name
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.gateway.arn
  desired_count          = var.desired_count
  launch_type            = "FARGATE"
  enable_execute_command = false
  propagate_tags         = "SERVICE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.target_group_arn
    container_name   = "gateway"
    container_port   = var.container_port
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  health_check_grace_period_seconds = 30
}

# ---- Ledger rebuild: hourly, raise-only ------------------------------------
# Runs the same image as `python -m app.rebuild`. If Valkey ever comes back lower than the spend in
# the audit logs (node loss, bad restore), counters are raised back. Never lowers anything.

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-ledger-rebuild-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["${aws_ecs_task_definition.gateway.arn_without_revision}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.this.arn]
    }
  }
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn, var.task_role_arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_scheduler_schedule" "ledger_rebuild" {
  name                = "${var.name}-ledger-rebuild"
  schedule_expression = var.ledger_rebuild_schedule

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn
    input = jsonencode({
      containerOverrides = [{ name = "gateway", command = ["python", "-m", "app.rebuild"] }]
    })

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.gateway.arn_without_revision
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = var.private_subnet_ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = false
      }
    }

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
