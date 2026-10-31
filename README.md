# secure-llm-gateway

An OpenAI-compatible gateway to Amazon Bedrock. Existing tools and SDKs point at it by changing two settings, the base URL and the key. It enforces:
- **who** may call: a Cognito JWT, checked at API Gateway and again in the gateway;
- **what** they may send: a strict schema;
- **which models** their team may use;
- **how much** they may spend: atomic monthly budgets in Valkey.

It runs in a VPC with **no internet egress**, and every Bedrock call carries a Guardrail that IAM won't let you skip.

Start with:
- **[docs/threat-model.md](docs/threat-model.md)**: threats, controls, tests and residual risks.
- **[docs/failure-modes.md](docs/failure-modes.md)**: what happens to the budget ledger under every failure, and the test that proves it.
- **[docs/evidence/](docs/evidence/)**: generated proof (test evidence, and after a deploy, the IAM simulation and guardrail probe).

```
client ─▶ WAF ─▶ API Gateway REST (Cognito authorizer) ─VPC Link v2─▶ internal ALB ─▶ FastAPI on Fargate ─PrivateLink─▶ Bedrock Converse + Guardrail
                                                                                       ├─▶ Valkey (monthly budgets)
                                                                                       └─▶ SSM (policy)
```

## Using it

```python
from openai import OpenAI

client = OpenAI(base_url="https://<api-id>.execute-api.us-east-1.amazonaws.com/v1",
                api_key=cognito_access_token)        # scripts/cognito.sh token <user>
resp = client.chat.completions.create(model="fast", messages=[{"role": "user", "content": "hi"}])
```

Cognito access tokens last 15 minutes. Long-running apps renew them with the refresh token, so users don't have to sign in again.

| Endpoint | |
|---|---|
| `POST /v1/chat/completions` | the main call |
| `GET /v1/models` | only the aliases *this user's team* may use |

**Accepted fields**

| Field | Notes |
|---|---|
| `model` | alias only: `fast` (Claude Haiku 4.5), `smart` (Claude Sonnet 5.5); `open` (Llama 4 Maverick) is defined but in no team's allowlist until its prices are verified |
| `messages` | `system`, `user`, `assistant`; text only (string or text parts) |
| `max_tokens` | capped per model (`smart` 1024, `fast` 2048) so responses finish within API Gateway's 29 s timeout; drives the budget reservation |
| `temperature`, `top_p` | passed to Converse where the model supports them (`smart` takes neither; `fast` takes one or the other) |
| `stop` | passed through |

**Rejected fields**, each with a named error such as `unsupported_parameter: stream`:
- `stream: true`, until streaming is added
- `tools` and `tool_choice`, because tool calling is out of scope for v1
- `n > 1`, because it multiplies cost
- anything unknown (`unknown_parameter: api_base`), via `extra="forbid"`

**Responses** use the standard OpenAI shape, including `usage`, plus two headers:
- `X-Request-Id` ties a complaint to the metadata log line;
- `X-Budget-Remaining-USD` shows the lower of the user's and the team's remaining monthly budget.

Errors use OpenAI's error shape and carry `x-should-retry`, so SDKs retry throttling but not budget or validation failures.

## Request pipeline

| # | Check | Fails with |
|---|---|---|
| 1 | Verify JWT, read user and team from claims | 401 |
| 2 | Validate request shape | 400 |
| 3 | Resolve model alias against policy | 403 |
| 4 | Reserve budget in Valkey | 429 budget exceeded |
| 5 | Converse with Guardrail | 400 blocked, 429 throttled (Retry-After, not charged) |
| 6 | Settle actual cost, log metadata | - |
| 7 | Return | 200 |

If a dependency is down, the gateway returns 503 instead of letting requests through. If its configuration is missing, invalid, or has an allowed model without a price, it refuses to start.

## Budgets: reserve, then settle

A user with $0.10 left fires ten requests at once. If each one checked the balance, called the model and then deducted, all ten would see money and all ten would go through.

Instead, `reserve.lua` atomically holds the **maximum** each request could cost, before Bedrock is called. That maximum is the input upper bound plus `max_tokens` at the output price, and it is held against both of these keys:

```
slg:budget:team:{team}:{YYYY-MM}    slg:budget:user:{sub}:{YYYY-MM}    (UTC month, TTL 35 days)
```

`settle.lua` then swaps the hold for the real cost. If a worker dies mid-request, the reconciler (`reconcile.lua`) refunds the hold if Bedrock was never called, and otherwise keeps it charged and raises an alarm.

Retries that send an `Idempotency-Key` can't hold budget twice. Before any money is spent, the hold must be acknowledged by the required number of replicas, so a failover can't lose it.

Limits come from `policy.yaml`, read via SSM, with **user > team > default** precedence. Valkey runs with **cluster mode disabled** because the reserve touches both keys atomically, and in cluster mode those keys would hash to different slots.

## Layout

```
gateway/app/      main.py (pipeline) · auth.py (JWT) · schema.py (strict models) · policy.py (aliases,
                  prices, budgets, SSM store) · budget.py + reserve.lua/settle.lua · bedrock.py · logging.py
                  config.py (fail-closed startup checks)
gateway/policy.yaml   source of truth → SSM /slg-dev/policy
terraform/modules/    network · bedrock-access · gateway (ECR, Valkey, SSM, ECS) · edge (Cognito, REST API,
                      WAF, VPC link, internal ALB) · observability
deploy/valkey/users.acl   compose ACL (mirrors the Terraform ACL; a test enforces it)
docker-compose.yml        Valkey for local runs and CI
.github/workflows/ci.yml  tests (incl. Valkey concurrency as the least-privilege user) + terraform validate
scripts/              cognito.sh (users, tokens) · dev_token.py (local-only JWTs)
tests/unit/           auth · schema/policy · config · budget/Lua/Valkey · pipeline · OpenAI SDK
tests/abuse/          black-box abuse tests (-m live)
```

## Run locally

```bash
make hooks                        # once per clone: gitleaks pre-commit secret scan (brew install gitleaks)
make venv && make test            # no AWS or Redis needed
make evidence                     # + ledger, failover and ACL tests on compose Valkey; writes docs/evidence/ (needs Docker)
make run-local                    # ENVIRONMENT=local, dev-signed tokens, in-memory budget, real Bedrock
GATEWAY_URL=http://127.0.0.1:8080 GATEWAY_TOKEN=$(.venv/bin/python scripts/dev_token.py mint) make test-live
```

Dev tokens are disabled by default. The gateway refuses to start if `DEV_TOKENS_ENABLED=true` and `ENVIRONMENT` is anything other than `local`.

## Deploy

Run `make` from the repo root with `export AWS_PROFILE=<your-profile> AWS_PAGER=""`.

**Once per account:** create the Terraform state bucket and the account budget alert. These live in `terraform/bootstrap`, outside the gateway stack, so `make destroy` never removes them.

```bash
cp terraform/bootstrap/terraform.tfvars.example terraform/bootstrap/terraform.tfvars   # set budget_alert_email
# first run only: comment out `backend "s3" {}` in terraform/bootstrap/versions.tf
terraform -chdir=terraform/bootstrap init && terraform -chdir=terraform/bootstrap apply
# restore the backend block, then move the bootstrap state into the bucket it created
make bootstrap-init MIGRATE=1
```

**The gateway stack:** its state is in S3 (versioned, KMS-encrypted, S3-native locking).

```bash
make init                                                    # MIGRATE=1 once, to move existing local state
terraform -chdir=terraform/envs/dev apply -target=module.gateway.aws_ecr_repository.gateway -var image_tag=v0.3.1
make push && make apply                                      # TAG defaults to the Makefile value
make test-live-aws                                           # creates/uses a test user, runs the 23 abuse tests
make verify-iam verify-guardrail                             # IAM simulation + real calls from inside the VPC
```

Approximate dev cost in us-east-1:
- 6 interface endpoints × 2 AZs: ~$88/mo
- internal ALB: ~$18
- Valkey `cache.t4g.micro` × 2 (primary + Multi-AZ replica): ~$24
- Fargate ARM, 0.5 vCPU: ~$15
- WAF: ~$10 plus requests
- API Gateway, Cognito, KMS and logs: small

Run `make destroy` when you're done.
