# Failure modes and the budget invariant

The ledger must preserve, per team and per user, every month:

```
settled spend + outstanding holds  ==  ledger counter  <=  authorized budget
```

"Outstanding holds" covers holds still open (`reserved`, `invoking`) and holds the reconciler kept charged (`expired_charged`). Every failure below either keeps the equality exact or **over-counts**. Nothing in the design under-counts, because an under-count is the only way to overspend.

**The hold is a proven upper bound.** It is the input upper bound (UTF-8 bytes) plus `max_tokens`, priced at the model's rates, so a request can never cost more than its hold. That gives every outcome a safe answer:

| Outcome | What happens | Why it's safe |
|---|---|---|
| Bedrock responds | Settle the real cost, release the rest | exact |
| **Confirmed** failure before generation (throttle, 4xx, not ready, couldn't connect) | Release the hold; charge nothing | nothing was generated |
| **Unknown** outcome (read timeout, connection dropped mid-response, 5xx, model timeout, worker died mid-call) | Charge the hold | the real cost can't exceed it |

> We never guess an unknown outcome as zero. Unknown means charge the upper bound, because the reservation is a proven ceiling on cost.

The Bedrock client makes **no automatic retries** (`total_max_attempts = 1`). A hidden retry after a lost-but-billed response would bill twice against one hold. Clients retry explicitly, and each retry is reserved on its own.

Evidence: run `make evidence` to regenerate [`evidence/test-evidence.md`](evidence/test-evidence.md). After a deploy, [`evidence/iam-simulation.md`](evidence/iam-simulation.md) and [`evidence/guardrail-probe.md`](evidence/guardrail-probe.md) are produced by `make verify-iam` and `make verify-guardrail`.

## Hold lifecycle

```
reserve ─▶ reserved ──begin──▶ invoking ──settle(actual)──▶ settled
              │                   ├──settle(0)───────────▶ released          (model refused/failed, unbilled)
              │                   └──deadline + reconcile─▶ expired_charged  (keep worst case; alarm)
              │                   ├──settle(hold)────────▶ charged_unknown   (outcome unknown: charge the ceiling)
              ├──settle(0)──────────────────────────────▶ released          (failed before the model call)
              └──deadline + reconcile─────────────────────▶ expired_released  (refund)
```

- `begin` must succeed before Bedrock is called. A hold past its deadline can't begin, so the reconciler and a slow request can never both act on the same money.
- Each hold record carries the request id. Bedrock receives it as `requestMetadata.ledger_id`, so an `expired_charged` hold can be matched to Bedrock's invocation log and corrected. A late `settle` also corrects it automatically.

## Failure matrix

| # | Failure | What the gateway does | Invariant | Test |
|---|---|---|---|---|
| F1 | Ten concurrent requests race the last $0.10 | One Lua script checks and reserves against team + user atomically; exactly the affordable number win | exact | `test_concurrent_requests_cannot_overspend` (memory + Valkey), `test_concurrent_overspend_blocked_end_to_end` |
| F2 | Client retries (SDK auto-retry, network blip) | With `Idempotency-Key`: the second attempt gets `409 duplicate_request` (in flight) or `409 already_completed`. Keys are namespaced per user: ledger id = `sha256(sub ‖ \0 ‖ key)`, so another user's identical key is a separate request that is neither blocked nor revealed. Without a key, each attempt is a separate, separately budgeted request | exact | `test_duplicate_in_flight_and_completed`, `test_idempotency_key_*`, `test_cross_user_idempotency_key_collision`, `test_cross_user_same_key_holds_independently` |
| F3 | Worker dies after reserve, before the model call | Hold stays `reserved`. After the 300 s deadline the reconciler refunds it (`expired_released`) | exact | `test_crash_before_model_call_is_refunded` (ledger + app) |
| F4 | Worker dies during the model call | Hold stays `invoking`. The reconciler marks it `expired_charged`, keeping the worst case, and the `holds-expired-charged` alarm fires. A late settle corrects it | over-counts until corrected | `test_crash_during_model_call_stays_charged`, `test_worker_crash_mid_call_is_recovered_by_reconciler`, `test_late_settle_corrects_expired_charged` |
| F5 | Random mix of all the above, plus retries and reconciler runs | - | exact after every step; nothing left open at the end | `test_invariant_under_random_crashes_and_retries` (5 seeds × 2 stores) |
| F6 | Valkey failover right after a reservation | The hold must be on `REDIS_MIN_REPLICA_ACKS` replicas (`WAIT`) before Bedrock is called. The promoted replica has it | exact | `test_valkey_failover_keeps_acknowledged_holds` (injected `REPLICAOF NO ONE`) |
| F6b | Worker dies **after** the replica-acknowledged `invoking` write, **before** the Converse request leaves the process | Order is `reserve → begin(invoking) → WAIT → Converse`, so the state is durable first. The ledger can't distinguish this from "billed, answer lost", so the reconciler keeps the hold charged (`expired_charged`), including after a failover | over-counts (safe) | `test_preinvoke_state_is_replica_acknowledged_before_bedrock_call` (call order), `test_crash_between_durable_preinvoke_write_and_bedrock_call`, `test_valkey_crash_after_durable_preinvoke_write_then_failover` |
| F7 | Replica lagging or down (`WAIT` times out after 200 ms, or errors) | Hold released, `503 dependency_unavailable` (`x-should-retry: true`, `Retry-After: 5`); Bedrock never called; `replica-ack-failures` alarm (threshold 1/min) | exact | `test_hold_not_replicated_fails_closed`, `test_wait_itself_erroring_fails_closed`, `test_valkey_wait_timeout_fails_closed_end_to_end` (real `WAIT` on compose Valkey) |
| F8 | Valkey unreachable | `503 dependency_unavailable` before Bedrock | exact | `test_redis_down_fails_closed_503` |
| F9 | Settle can't reach Valkey | The hold stays open; the reconciler closes it later | over-counts | (F3/F4 paths) |
| F10 | Bedrock throttles | `429` + `Retry-After`; hold released; not charged | exact | `test_throttle_429_retry_after_and_not_charged` |
| F11 | Bedrock refuses before generation (`ServiceUnavailable`, `ModelNotReady`, 4xx, connect failure) | `503`/`502`; hold released | exact | `test_confirmed_unbilled_failures_release_hold`, `test_connect_failure_is_confirmed_unbilled` |
| F11b | **Unknown outcome**: read timeout, connection dropped, `InternalServer`, `ModelTimeout`, `ModelError`, any other 5xx | `502 upstream_outcome_unknown`, charged the full hold (`charged_unknown`), `x-should-retry: false`; `UnknownOutcomes` metric | over-counts (by design) | `test_unknown_outcome_charges_the_hold` (6 cases) |
| F11c | Hidden SDK retry bills twice against one hold | botocore retries disabled for Converse | prevented | `test_bedrock_client_never_retries_silently` |
| F12 | Guardrail blocks | `400 blocked_by_guardrail`; settled at the **actual** usage Bedrock reports | exact | `test_guardrail_block_400_and_charged` |
| F13 | Policy missing or invalid at startup, or an allowed model has no price | The process refuses to start | - | `test_build_refuses_*`, `test_invalid_policy_refused` |
| F14 | Policy refresh missing or invalid while running | Keeps enforcing the last **validated** policy, so a broken update never removes controls. The `policy-refresh-rejected` alarm fires **immediately**. After `POLICY_MAX_STALE_S` (900 s, the 15-minute revocation window) with no valid refresh, every request gets `503 policy_unavailable` (`policy-stale` alarm), so a stale policy can't stay in force indefinitely | - | `test_rejected_refresh_raises_alarm_immediately`, `test_bad_policy_refresh_then_stale_policy_rejects_every_request` |
| F15 | Dev-token mode enabled outside `ENVIRONMENT=local` | The process refuses to start | - | `test_dev_tokens_refused_outside_local` |
| F16 | Bedrock call made without our guardrail, by any code holding the task role | IAM explicit deny | - | `scripts/verify_iam.py` (policy logic), `scripts/verify_guardrail.sh` (real calls; post-deploy) |
| F17 | **Valkey loses its data** (node replaced, bad restore, flush) | Prevented first: 1 replica, Multi-AZ, automatic failover by default, and Terraform refuses 0 replicas unless explicitly allowed. Recovered second: an hourly job sums the month's spend from the audit logs and **raises** any lower counter, never lowering one; the `ledger-lost-spend` alarm fires | restored to ≥ logged spend | `test_disaster_drill_valkey_loses_everything`, `test_rebuild_restores_enforcement`, `test_rebuild_never_lowers_the_ledger`, `test_raise_floor_only_raises` (both stores) |

## Known limits

- **`WAIT` is not synchronous replication.** With one replica and `WAIT 1`, an acknowledged hold survives a failover to that replica. With more replicas, ElastiCache could promote one that hadn't acknowledged yet. Keep `REDIS_MIN_REPLICA_ACKS` equal to the replica count. Dev runs with 0 replicas, so a node loss means a short outage (503s) rather than a lost ledger. Snapshots are kept for 3 days.
- **The rebuild lags by up to an hour** (schedule) plus log-ingestion delay. Spend made between data loss and the rebuild is still capped per request by the holds, but not by the monthly total.
- **F6 and F7 are proven in compose and CI**, not yet on ElastiCache.
- **Idempotency doesn't store responses** (metadata-only by design). A retry after a completed request gets `409 already_completed` rather than the original answer.
- **`expired_charged` correction is manual** for now: match the hold's `ledger_id` against the Bedrock invocation log. Automating that is the next step.
- **The policy is not cryptographically signed.** Its integrity rests on IAM: only the deployer can write the SSM parameter, and the task can only read it, as `scripts/verify_iam.py` checks.
