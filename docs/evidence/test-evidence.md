# Test evidence

Generated 2026-10-31 20:00 UTC by `scripts/evidence.py` (Python 3.14.2, budget store: valkey 8.1.11).

**302 passed, 0 failed, 0 skipped.** Ledger tests run against both the in-memory store and the real Lua scripts on Valkey, as the least-privilege `slg-gateway` user.

| Behaviour demonstrated | Tests | Result |
|---|---|---|
| Budget invariant holds under random crashes, retries and reconciler runs | `test_invariant_under_random_crashes_and_retries` (10 cases) | ✅ |
| 10 concurrent requests cannot overspend ($0.10 limit, $0.04 holds -> exactly 2) | `test_concurrent_overspend_blocked_end_to_end`, `test_concurrent_requests_cannot_overspend` (3 cases) | ✅ |
| Crash before the model call -> hold refunded by the reconciler | `test_crash_before_model_call_is_refunded` (3 cases) | ✅ |
| Crash during the model call -> hold kept (never under-count), alarmed, reconcilable | `test_crash_during_model_call_stays_charged`, `test_worker_crash_mid_call_is_recovered_by_reconciler` (3 cases) | ✅ |
| Retries with the same Idempotency-Key never hold budget twice | `test_duplicate_in_flight_and_completed`, `test_idempotency_key_blocks_double_spend`, `test_idempotency_key_in_flight_conflict` (4 cases) | ✅ |
| Valkey failover keeps acknowledged (WAIT-replicated) holds | `test_valkey_failover_keeps_acknowledged_holds`, `test_valkey_replica_ack_gate` (2 cases) | ✅ |
| WAIT timeout (hold not replica-acknowledged) -> 503, Bedrock never called, hold released, alarmed | `test_hold_not_replicated_fails_closed`, `test_valkey_wait_timeout_fails_closed_end_to_end`, `test_wait_itself_erroring_fails_closed` (3 cases) | ✅ |
| Pre-invoke state is replica-acknowledged before Bedrock; crash between that write and the call stays charged | `test_crash_between_durable_preinvoke_write_and_bedrock_call`, `test_preinvoke_state_is_replica_acknowledged_before_bedrock_call`, `test_valkey_crash_after_durable_preinvoke_write_then_failover` (3 cases) | ✅ |
| Idempotency-Key namespaced per user (cross-user collisions are independent) | `test_cross_user_idempotency_key_collision`, `test_cross_user_same_key_holds_independently`, `test_idempotency_ids_are_scoped_per_user`, `test_idempotency_namespace_is_the_stable_subject_not_username` (5 cases) | ✅ |
| Full revocation within 15 minutes (token lifetime == policy staleness bound) | `test_default_lifetime_matches_15_minute_tokens`, `test_policy_staleness_defaults_to_revocation_window` (2 cases) | ✅ |
| Valkey unreachable -> 503 before any spend | `test_redis_down_fails_closed_503` (1 cases) | ✅ |
| Missing/invalid policy or unpriced allowed model -> process refuses to start | `test_build_refuses_missing_policy`, `test_build_refuses_unpriced_model`, `test_invalid_policy_refused` (12 cases) | ✅ |
| Bad policy refresh -> last valid policy for <= max staleness, then 503 for every request | `test_bad_policy_refresh_then_stale_policy_rejects_every_request` (1 cases) | ✅ |
| Dev-signed tokens refused outside ENVIRONMENT=local | `test_build_refuses_to_start_with_dev_tokens_outside_local`, `test_dev_tokens_refused_outside_local` (6 cases) | ✅ |
| Team comes only from cognito:groups; client headers, body fields and self-edited attributes ignored | `test_client_supplied_team_body_field_rejected`, `test_client_supplied_team_headers_ignored`, `test_self_edited_attributes_do_not_change_team_or_identity`, `test_team_claim_is_not_configurable` (11 cases) | ✅ |
| JWT forgery rejected (alg=none, HS256 key confusion, tampering, unknown kid) | `test_alg_none_rejected`, `test_hs256_key_confusion_rejected`, `test_tampered_payload`, `test_unknown_kid` (4 cases) | ✅ |
| Routing/credential smuggling rejected by construction (api_base, provider, modelId) | `test_rejected_fields_400`, `test_unknown_parameter` (20 cases) | ✅ |
| Bedrock throttling -> 429 + Retry-After, not charged | `test_throttle_429_retry_after_and_not_charged` (1 cases) | ✅ |
| Unknown Bedrock outcome (timeout, dropped connection, 5xx) -> charged the hold, never zero | `test_unknown_outcome_charges_the_hold` (6 cases) | ✅ |
| No hidden SDK retries that could bill twice against one hold | `test_bedrock_client_never_retries_silently` (1 cases) | ✅ |
| Valkey loses all data -> hourly rebuild restores spend from audit logs; never lowers the ledger | `test_disaster_drill_valkey_loses_everything`, `test_raise_floor_only_raises`, `test_rebuild_never_lowers_the_ledger`, `test_rebuild_restores_enforcement` (5 cases) | ✅ |
| Rejected policy refresh keeps controls and alarms immediately | `test_rejected_refresh_raises_alarm_immediately` (1 cases) | ✅ |
| Production Valkey ACL == CI ACL | `test_acl_matches_terraform` (1 cases) | ✅ |
| Gateway's Valkey user cannot FLUSHALL, KEYS, CONFIG, SCAN, REPLICAOF, ACL or touch non-slg keys | `test_valkey_gateway_user_is_least_privilege` (10 cases) | ✅ |
| Guardrail probe logic detects a missing IAM guardrail deny | `test_probe_fails_when_guardrail_not_enforced` (1 cases) | ✅ |
