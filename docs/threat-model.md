# Threat Model — Secure LLM Gateway

**Scope:** the dev deployment in `terraform/envs/dev`. Authenticated users (Cognito, one team each) call an OpenAI-compatible API subset. The gateway enforces identity, request shape, per-team model policy and monthly spend budgets, then calls Amazon Bedrock Converse with a Guardrail attached.
**Method:** STRIDE per trust boundary, cross-referenced to the OWASP Top 10 for LLM Applications (2025).
**Last reviewed:** 2026-10-31

## 1. System

```
client ─TLS─▶ WAF ─▶ API Gateway REST API ──VPC Link v2──▶ internal ALB ──▶ FastAPI gateway (Fargate) ──PrivateLink──▶ Bedrock Converse
                     Cognito user pool authorizer          2 paths only     private subnets, no NAT                   + Guardrail
                     stage "v1", throttling                                  │
                                                                             ├──▶ Valkey (budget ledger; TLS + ACL user)
                                                                             ├──▶ SSM Parameter Store (policy)        via VPCE
                                                                             ├──▶ Secrets Manager (Valkey password)  via VPCE
                                                                             └──▶ CloudWatch Logs (metadata only)    via VPCE
```

Clients use stock OpenAI SDKs with `base_url = https://<api-id>.execute-api.us-east-1.amazonaws.com/v1` and `api_key = <Cognito access token>`.

### Request pipeline

Each step either passes the request on or stops it with the response shown.

| # | Step | Fails with |
|---|---|---|
| 1 | Verify JWT, read user + team from claims | `401 unauthorized` |
| 2 | Validate request shape | `400 unsupported_parameter` (stream, tools, tool_choice, n>1, images), `400 unknown_parameter` (anything else), `400 invalid_request` |
| 3 | Resolve model alias (`fast`/`smart`/`open`) against team policy; sampling support; credential/PII screening | `403 model_not_allowed`/`team_not_allowed`/`max_tokens_not_allowed`, `400 unsupported_parameter`, `400 sensitive_data` |
| 4 | Reserve worst-case cost against **monthly** team + user budgets (Lua), plus per-user rpm | `429 budget_exceeded` (not retryable), `429 rate_limited` (Retry-After), `503 dependency_unavailable` |
| 5 | Bedrock Converse with Guardrail | `400 blocked_by_guardrail`, `429 upstream_throttled` (Retry-After, not charged), `503 upstream_unavailable`, `502 upstream_error` |
| 6 | Settle actual cost, write metadata log | — |
| 7 | Return | `200` + `X-Request-Id`, `X-Budget-Remaining-USD` |

Errors use the OpenAI shape `{"error": {message, type, code, param}}` and carry `x-should-retry`, so SDKs don't auto-retry budget or validation failures.

### Assets

1. **Bedrock spend.**
2. **Prompt and completion content.**
3. **User credentials and access tokens.**
4. **The task role's AWS credentials.**
5. **Budget ledger and policy integrity.** If either can be under-counted or loosened, overspend follows.
6. **Image integrity.**
7. **Audit trail.**

### Trust boundaries

- **B1 Internet → WAF/API Gateway:** untrusted.
- **B2 API Gateway → gateway:** the token has been checked once. The network path inside the VPC is not trusted.
- **B3 Gateway → Valkey, SSM, Bedrock:** dependencies may fail. Model output is untrusted.
- **B4 Operator → AWS control plane:** Terraform, ECR, Cognito admin, SSM.

## 2. Threats and controls

Each threat is tagged with its STRIDE category and its OWASP LLM ID. The Test column names the file under `tests/` that covers it.

### B1 — Internet → edge

| # | Threat | Controls | Test |
|---|---|---|---|
| T1 | **S** Unauthenticated access | The Cognito user pool authorizer on both methods requires the `aws.cognito.signin.user.admin` scope, so only access tokens pass. That scope also lets users edit their own attributes, which is safe only because team never comes from an attribute (T8a), and the app client may write only `name`. Unknown paths return 404 (`MISSING_AUTHENTICATION_TOKEN` is remapped). Gateway responses use the OpenAI error shape | `abuse/test_live_gateway.py::test_401` |
| T2 | **S** Credential stuffing or account takeover | WAF IP-reputation list and per-IP rate limits. Cognito: admin-created users only, 14-character passwords, optional TOTP MFA, admin-only recovery, `prevent_user_existence_errors` | — |
| T3 | **D** Floods (LLM10 Unbounded Consumption) | WAF rate limits per IP (600 per 5 min) and per `Authorization` header (300 per 5 min). Stage throttling at 20 rps steady and 40 burst. Per-user rpm is enforced atomically in Valkey. Spend is capped by monthly budgets (T9) | `unit/test_budget.py::test_rate_limit` |
| T4 | **D** Oversized bodies | WAF blocks `Content-Length` ≥ 300 kB. The gateway rejects bodies over 256 KiB, over 32k characters of content, or over 50 messages | `test_oversized_body_400` |
| T5 | **I** Bodies or tokens in logs | Stage `data_trace_enabled = false`. The access log format has no body. WAF logs redact `Authorization` | — |

### B2 — API Gateway → gateway

| # | Threat | Controls | Test |
|---|---|---|---|
| T6 | **S** Someone inside the VPC calls the ALB directly and skips the authorizer | The gateway re-verifies every token: RS256 only, `kid` must be in JWKS, and it checks `iss`, `exp`, `iat`, `token_use=access`, `client_id`, exactly one team group, and a maximum lifetime equal to Cognito's (15 minutes, passed in by Terraform). The ALB SG accepts only the VPC-link SG. An auth-failure alarm fires because the authorizer normally filters these first | `unit/test_auth.py`, `test_pipeline.py::test_401` |
| T7 | **S** JWT forgery: `alg=none`, HS256 with the public key, unknown key, tampered claims, team escalation | Algorithm allowlist. The key comes only from JWKS by `kid`. Signature is verified before any claim is used | `test_alg_none_rejected`, `test_hs256_key_confusion_rejected`, `test_tampered_payload` |
| T8a | **E** **A user moves themselves into a richer team** by editing a profile attribute | Team is read **only** from `cognito:groups`, which only admin APIs can change. The claim name is hard-coded, and setting `TEAM_CLAIM` makes the process refuse to start. Identity is `username` (immutable) and `sub`, never `email`, `preferred_username` or `custom:*`. The pool defines no custom attributes, and the app client's `write_attributes = ["name"]` | `test_self_edited_attributes_do_not_change_team_or_identity`, `test_custom_attribute_alone_grants_no_team`, `test_self_edited_team_attribute_cannot_unlock_models_or_budget`, `test_team_claim_is_not_configurable` |
| T8 | **S** **Dev-signed tokens accepted in a real environment** | Dev tokens are off by default. `DEV_TOKENS_ENABLED=true` with `ENVIRONMENT≠local`, or with `ENVIRONMENT` unset, makes the process refuse to start. The dev issuer is also refused outside local. Terraform's `environment` variable rejects `"local"` | `unit/test_config.py::test_dev_tokens_refused_outside_local`, `test_build_refuses_to_start_with_dev_tokens_outside_local` |
| T9 | **D** Overspend through concurrency, retries, crashes or failover | **Reservation ledger** (see [failure-modes.md](failure-modes.md)). One Lua script atomically reserves the worst-case cost against both the team and user **monthly** budgets (`slg:budget:{team\|user}:{id}:{YYYY-MM}`, TTL 35 days). Each hold moves through `reserved → invoking → settled \| released`. The reconciler closes holds whose worker died: refund if Bedrock was never called, keep the charge if it might have been. `Idempotency-Key` retries can't hold budget twice. Before any spend, the hold must be acknowledged by `REDIS_MIN_REPLICA_ACKS` replicas (`WAIT`). The invariant `settled + outstanding == counter ≤ limit` is checked after every step of a randomized crash test, on both stores | `test_ledger.py` (all scenarios × in-memory and Valkey), `test_valkey_failover_keeps_acknowledged_holds`, `test_worker_crash_mid_call_is_recovered_by_reconciler` |
| T10 | **T** Key injection via identity values | Budget ids are the token `sub` or validated team names. Anything outside `[A-Za-z0-9._@-]{1,128}` is hashed | `test_unsafe_ids_are_hashed` |
| T11 | **E** Routing abuse: `api_base`, `provider`, `modelId`, `guardrailConfig` | `extra="forbid"` plus `strict=True`. Unknown fields fail by construction (`unknown_parameter: api_base`), not through a denylist. Raw Bedrock model IDs are not aliases | `test_unknown_parameter`, `test_rejected_fields_400`, `test_openai_sdk.py::test_unknown_param_rejected` |
| T12 | **E** Cost multipliers: `n>1`, tools, streaming without output scanning, oversized `max_tokens` | Named `unsupported_parameter` rejections. `max_tokens` is capped per model and drives the reservation | `test_unsupported_parameter`, `test_per_model_max_tokens` |
| T13 | **E** Using models outside the team's allowlist | Per-team alias allowlist; "doesn't exist" and "not yours" return the same 403. IAM and the VPCE policy also limit Bedrock to the three allowlisted profiles | `test_resolve_denials` |
| T14 | **I** Credentials or PII sent to the model (LLM02) | Credentials are blocked. Email, SSN, Luhn-valid cards and US phone numbers are redacted before Bedrock, after NFKC and zero-width normalisation | `test_credentials_blocked`, `test_pii_redacted_before_bedrock` |
| T15 | **T** Prompt injection (LLM01); calls that skip the guardrail | Guardrail `PROMPT_ATTACK` filter on every Converse call. IAM **explicitly denies** `InvokeModel` and `InvokeModelWithResponseStream` (which Converse and ConverseStream authorize as) unless `bedrock:GuardrailIdentifier` is our guardrail's ARN. The gateway sends the ARN, and startup refuses a non-ARN value. Other entry points (async invoke, batch jobs, agents, knowledge bases) aren't granted at all. No tools are exposed (LLM06). Evidence: an IAM simulation matrix, plus real Converse calls from a one-off task inside the VPC | `test_guardrail_block_400_and_charged`, `scripts/verify_iam.py`, `scripts/verify_guardrail.sh`, `test_probe.py` |
| T16 | **I** Errors leak internals or echo input | Validation errors name the field only, never the value. Bedrock error codes are logged, not returned. Auth failures return one generic message | `test_rejections_do_not_echo_values`, `test_upstream_failures_release_hold` |

### B3 — Dependencies and output

| # | Threat | Controls | Test |
|---|---|---|---|
| T17 | **T** Policy tampering or a bad policy loosens limits | The policy lives in SSM, published by Terraform from `gateway/policy.yaml` in git. **It is not cryptographically signed.** Integrity rests on IAM: only the deployer can write the parameter, and the task role can only read that one parameter (checked by `verify_iam.py`). Validation is strict: unknown keys are rejected, every allowed model needs positive prices, and a default budget is required. **At startup**, a missing or invalid policy stops the process. **While running**, an invalid refresh keeps the last validated policy and alarms immediately; after 900 s (15 min) without a valid refresh, every request gets `503 policy_unavailable`. The `policy-stale` alarm fires | `test_invalid_policy_refused`, `test_build_refuses_unpriced_model`, `test_bad_policy_refresh_then_stale_policy_rejects_every_request` |
| T18 | **D** Valkey down, lagging or failing over | 503 before Bedrock is called. A hold not acknowledged by the required replicas within the `WAIT` timeout is released and 503'd; the `replica-ack-failures` alarm fires. A failed settle leaves the hold for the reconciler (over-count, never under-count) | `test_redis_down_fails_closed_503`, `test_hold_not_replicated_fails_closed`, `test_valkey_replica_ack_gate` |
| T19 | **D** Throttling charges users for nothing | 429 with `Retry-After` and `x-should-retry: true`; the hold is released at $0 | `test_throttle_429_retry_after_and_not_charged` |
| T20 | **I** Output leaks secrets or PII (LLM02, LLM05) | Guardrail output filters, then local redaction. The output-redaction alarm fires at threshold 1 | `test_output_secrets_redacted` |
| T21 | **E** A compromised task uses its role elsewhere | The role can call `InvokeModel*` only on allowlisted profiles, via our VPCE, with the guardrail; plus `ssm:GetParameter` on one parameter. No NAT. SG egress is limited to the VPCE SG, the S3 prefix list, the Valkey SG and the VPC resolver. Execution and task roles are separate | flow-log `FlowRejects` |
| T22 | **T** Ledger tampering from elsewhere in the VPC | The Valkey SG admits only the task SG. TLS is required. The `default` user is disabled. The app user is limited to `~slg:*` and the commands the scripts need; it cannot `FLUSHALL`, `KEYS`, `SCAN`, `CONFIG`, `REPLICAOF` or `ACL`. The compose ACL mirrors it, CI runs the ledger as that user, and a test fails if the two drift | `test_acl_matches_terraform`, `test_valkey_gateway_user_is_least_privilege` |
| T23 | **R** Disputes over who spent what | The `X-Request-Id` a client sees matches the `request_id` in a metadata-only log line (user, team, model, tokens, reserved and actual USD, code). API Gateway access logs record username and IP | `test_request_id_on_errors_and_in_logs`, `test_logs_are_metadata_only` |

### B4 — Operator and supply chain

| # | Threat | Controls |
|---|---|---|
| T24 | **T** Vulnerable dependency (LLM03) | Seven pinned dependencies, no LLM framework. ECR tags are immutable, images scanned on push and KMS-encrypted |
| T25 | **I** Secrets in Terraform state | The Valkey passwords come from `random_password`, so they are in state. Use an encrypted, access-restricted S3 backend outside dev |
| T26 | **I** Prompts persisted | The logger allowlists field names. Bedrock invocation logging is metadata-only. All log groups use the CMK |

## 2a. Revocation: fully enforced within 15 minutes

**Guarantee:** any revocation is fully enforced within **15 minutes** (`revocation_window_minutes`, default 15). One Terraform variable drives both bounds below, so they can't drift apart.

| What is revoked | How | Enforced within | Why |
|---|---|---|---|
| A user (left, compromised) | `scripts/cognito.sh disable-user` (disable + global sign-out) | **≤ 15 min** | Refresh fails at once. The last access token expires within its 15-minute lifetime, and the gateway rejects any token whose `exp - iat` exceeds that |
| A user's team membership (moved or removed from a group) | Cognito admin group change | **≤ 15 min** | `cognito:groups` is fixed in the token until it expires (≤ 15 min). The new team applies at the next refresh |
| A model or a team (policy change) | Edit `policy.yaml` → `terraform apply` → SSM | **≤ 60 s** normally (refresh interval) | Each task reloads the policy every 60 s |
| …the same, if the new policy can't be loaded (invalid, SSM unreachable) | — | **≤ 15 min** | The last valid policy keeps being enforced and alarms immediately. After `POLICY_MAX_STALE_S` = 900 s without a valid refresh, **every** request is rejected (`503 policy_unavailable`), so a stale policy can never outlive the window |
| A user's or team's remaining budget, **by spending it** | Usage reaches the limit | **immediately** (next reservation) | Checked atomically against the ledger on every request; requests already holding a reservation finish within their hold |
| A user's or team's budget **limit** (lowered, or set to 0) | Edit `budgets` in `policy.yaml` → `terraform apply` → SSM | **≤ 60 s** normally; **≤ 15 min** if the new policy can't be loaded | A limit is policy: it takes effect at the next policy refresh (60 s). If the refresh fails, the last valid limit stays in force until `POLICY_MAX_STALE_S` (900 s), after which every request is rejected |
| An OAuth app client | Remove it from `JWT_CLIENT_IDS` / Cognito | **≤ 15 min** | Its issued tokens expire within their lifetime |

Not covered by the window: a request already in flight finishes, which takes at most ~29 s at API Gateway. JWKS rotation is a key-management event, not a revocation; see R3.

Tests:
- `test_default_lifetime_matches_15_minute_tokens` (a 901-second token is rejected)
- `test_token_lifetime_defaults_to_15_minutes`
- `test_policy_staleness_defaults_to_revocation_window`
- `test_bad_policy_refresh_then_stale_policy_rejects_every_request`
- `test_lowered_budget_limit_applies_at_next_refresh`
- `test_budget_exceeded_429_not_retryable` (spending to the limit is immediate)

## 3. Residual risks

| ID | Risk | Status / next step |
|---|---|---|
| R1 | REST API integration timeout is 29 s. Long completions return 504 while the gateway finishes and still settles the cost | Caps are sized to finish in time (`smart` 1024, `fast` 2048). Later: response streaming (up to 15 minutes) or a higher regional integration timeout |
| R2 | API Gateway → ALB is plain HTTP inside the VPC (bearer token on the wire) | Add an HTTPS listener with a private certificate |
| R3 | JWKS is baked at deploy | Re-apply after any Cognito key rotation, or set `JWKS_URL` with a `cognito-idp` endpoint |
| R4 | Revocation is bounded, not instant: up to 15 minutes (see Revocation section) | Accepted for v1. Next: a `jti`/username denylist in Valkey checked in step 1, for instant revocation |
| R5 | The input estimate (UTF-8 bytes) over-reserves by roughly 3–4×, so users are refused a little early at month end | Fail-safe by design. Next: Bedrock `CountTokens` |
| R6 | Not yet verified on AWS: VPC Link v2 + REST → ALB with `integration_target`; the Cognito scope check accepting password-flow access tokens; ElastiCache honouring the ACL and `WAIT` as written | First deploy: `make test-live` against `api_base_url` |
| R7 | The IAM guardrail deny depends on what Bedrock puts in `bedrock:GuardrailIdentifier` at runtime. Simulation can't show that | First deploy: `make verify-guardrail`, which makes real calls with and without the guardrail from inside the VPC |
| R14 | Unknown outcomes (`charged_unknown`, `expired_charged`) charge the full hold, so users can lose up to one request's ceiling per incident | Deliberate: never guess zero. Counted (`UnknownOutcomes`, `HoldsCharged`). Next: refund the difference automatically from Bedrock invocation logs (`requestMetadata.ledger_id`) |
| R15 | Retrying an already-completed `Idempotency-Key` returns 409, not the original response (responses are never stored) | Deliberate: metadata-only. Clients should treat 409 `already_completed` as "check your own records" |
| R8 | Prices in `policy.yaml` are first-party list prices (Claude) or placeholders (Llama) | `open` (Llama) is in no team's allowlist until verified, and a test enforces this on the production policy. Check the Claude prices against Bedrock pricing |
| R9 | Cluster mode is off on purpose: Serverless/cluster mode would put the team and user keys in different slots and break the atomic reserve, so auto-scaling was traded for correctness. Losing the node would reset every budget to zero, an under-count | 1 replica + Multi-AZ by default; Terraform refuses 0 without `allow_unreplicated_ledger`; `WAIT` for the replica before spending; an hourly raise-only rebuild from the audit logs; `ledger-lost-spend` alarm |
| R10 | The regex screening is US-centric | The Guardrail adds coverage. Localise per deployment |
| R11 | DNS to the VPC resolver could carry low-bandwidth exfiltration | Route 53 Resolver DNS Firewall |
| R12 | `X-Client-Ip` can be spoofed from inside the VPC | Logs only, never used for decisions |
| R13 | `aws_api_gateway_account` sets an account-wide logging role | Set `manage_apigw_account_logging_role = false` if another stack owns it |

## 4. Verification

- `make test`: unit, pipeline, config, OpenAI-SDK and in-memory ledger tests. No AWS or Redis needed.
- `make test-redis` / `make evidence`: everything, including the ledger scenarios on real Valkey (primary + replica) as the least-privilege user, the failover injection, and the ACL denials. CI runs it with `REQUIRE_REDIS=1` and uploads `docs/evidence/test-evidence.md`.
- `make verify-iam` and `make verify-guardrail` (post-deploy) write `docs/evidence/iam-simulation.md` and `guardrail-probe.md`. Last local run on 2026-10-31 (Valkey 8.1.11): 213 passed, 0 skipped; 10 concurrent $0.04 holds against $0.10 gave exactly 2 winners; `FLUSHALL`, `KEYS`, `CONFIG` and writes outside `slg:*` were denied for the gateway user.
- `make test-live`: about 23 black-box cases against the local app or `api_base_url`.
- After a deploy, check by hand: R6, R7, no internet egress from inside a task, and a WAF block showing up in the logs.
