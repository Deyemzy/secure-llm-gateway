#!/usr/bin/env python3
"""Run the test suite and write docs/evidence/test-evidence.md: which failure and abuse behaviours
were demonstrated, against what, and when. Exit code is pytest's.

    make evidence      (starts compose Valkey primary + replica first)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Behaviour -> test-name substrings that demonstrate it. Every listed behaviour must have >=1 passing test.
CLAIMS = [
    ("Budget invariant holds under random crashes, retries and reconciler runs",
     ["test_invariant_under_random_crashes_and_retries"]),
    ("10 concurrent requests cannot overspend ($0.10 limit, $0.04 holds -> exactly 2)",
     ["test_concurrent_requests_cannot_overspend", "test_concurrent_overspend_blocked_end_to_end"]),
    ("Crash before the model call -> hold refunded by the reconciler",
     ["test_crash_before_model_call_is_refunded"]),
    ("Crash during the model call -> hold kept (never under-count), alarmed, reconcilable",
     ["test_crash_during_model_call_stays_charged", "test_worker_crash_mid_call_is_recovered_by_reconciler"]),
    ("Retries with the same Idempotency-Key never hold budget twice",
     ["test_duplicate_in_flight_and_completed", "test_idempotency_key_blocks_double_spend",
      "test_idempotency_key_in_flight_conflict"]),
    ("Valkey failover keeps acknowledged (WAIT-replicated) holds",
     ["test_valkey_failover_keeps_acknowledged_holds", "test_valkey_replica_ack_gate"]),
    ("WAIT timeout (hold not replica-acknowledged) -> 503, Bedrock never called, hold released, alarmed",
     ["test_hold_not_replicated_fails_closed", "test_wait_itself_erroring_fails_closed",
      "test_valkey_wait_timeout_fails_closed_end_to_end"]),
    ("Pre-invoke state is replica-acknowledged before Bedrock; crash between that write and the call stays charged",
     ["test_preinvoke_state_is_replica_acknowledged_before_bedrock_call",
      "test_crash_between_durable_preinvoke_write_and_bedrock_call",
      "test_valkey_crash_after_durable_preinvoke_write_then_failover"]),
    ("Idempotency-Key namespaced per user (cross-user collisions are independent)",
     ["test_cross_user_idempotency_key_collision", "test_idempotency_namespace_is_the_stable_subject_not_username",
      "test_cross_user_same_key_holds_independently", "test_idempotency_ids_are_scoped_per_user"]),
    ("Full revocation within 15 minutes (token lifetime == policy staleness bound)",
     ["test_policy_staleness_defaults_to_revocation_window", "test_default_lifetime_matches_15_minute_tokens"]),
    ("Valkey unreachable -> 503 before any spend", ["test_redis_down_fails_closed_503"]),
    ("Missing/invalid policy or unpriced allowed model -> process refuses to start",
     ["test_build_refuses_unpriced_model", "test_build_refuses_missing_policy", "test_invalid_policy_refused"]),
    ("Bad policy refresh -> last valid policy for <= max staleness, then 503 for every request",
     ["test_bad_policy_refresh_then_stale_policy_rejects_every_request"]),
    ("Dev-signed tokens refused outside ENVIRONMENT=local", ["test_dev_tokens_refused_outside_local",
                                                            "test_build_refuses_to_start_with_dev_tokens_outside_local"]),
    ("Team comes only from cognito:groups; client headers, body fields and self-edited attributes ignored",
     ["test_self_edited_attributes_do_not_change_team_or_identity", "test_client_supplied_team_headers_ignored",
      "test_client_supplied_team_body_field_rejected", "test_team_claim_is_not_configurable"]),
    ("JWT forgery rejected (alg=none, HS256 key confusion, tampering, unknown kid)",
     ["test_alg_none_rejected", "test_hs256_key_confusion_rejected", "test_tampered_payload", "test_unknown_kid"]),
    ("Routing/credential smuggling rejected by construction (api_base, provider, modelId)",
     ["test_unknown_parameter", "test_rejected_fields_400"]),
    ("Bedrock throttling -> 429 + Retry-After, not charged", ["test_throttle_429_retry_after_and_not_charged"]),
    ("Unknown Bedrock outcome (timeout, dropped connection, 5xx) -> charged the hold, never zero",
     ["test_unknown_outcome_charges_the_hold"]),
    ("No hidden SDK retries that could bill twice against one hold", ["test_bedrock_client_never_retries_silently"]),
    ("Valkey loses all data -> hourly rebuild restores spend from audit logs; never lowers the ledger",
     ["test_disaster_drill_valkey_loses_everything", "test_rebuild_restores_enforcement",
      "test_rebuild_never_lowers_the_ledger", "test_raise_floor_only_raises"]),
    ("Rejected policy refresh keeps controls and alarms immediately", ["test_rejected_refresh_raises_alarm_immediately"]),
    ("Production Valkey ACLs == CI ACLs (gateway and drill users)", ["test_acl_matches_terraform"]),
    ("Gateway's Valkey user cannot FLUSHALL, KEYS, CONFIG, SCAN, REPLICAOF, ACL or touch non-slg keys",
     ["test_valkey_gateway_user_is_least_privilege"]),
    ("Guardrail probe logic detects a missing IAM guardrail deny", ["test_probe_fails_when_guardrail_not_enforced"]),
    ("Gateway's Valkey user refused DEL, UNLINK, GETDEL and HDEL; ledger state unchanged; ACL grants no delete command",
     ["test_valkey_gateway_user_cannot_delete", "test_gateway_acl_grants_no_delete_command"]),
    ("Drill Valkey user confined to budget counters (no holds, INCRBY, EVAL, FLUSHALL, KEYS, SCAN, CONFIG, ACL, WAIT)",
     ["test_valkey_drill_user_is_confined_to_budget_counters", "test_valkey_drill_runs_as_drill_user"]),
    ("Ledger drill: exact restore required, refuses (changing nothing) on open holds or outside dev",
     ["test_drill_wipes_and_restores_exactly", "test_drill_fails_when_rebuild_does_not_restore",
      "test_drill_refuses_with_open_hold_and_changes_nothing", "test_drill_refused_outside_dev"]),
    ("UTC month boundary (frozen clock): fresh budget at Nov 1 00:00:00, October kept; cross-midnight hold settles "
     "against October", ["test_budget_resets_at_utc_month_boundary",
                         "test_hold_reserved_in_october_settles_against_october", "test_key_format_is_monthly_utc"]),
    ("Bedrock AccessDenied -> 502 upstream_access_denied, not retryable, hold released (not charged)",
     ["test_model_access_denied_is_clean_and_not_charged"]),
    ("Dev-only live-test overlay can only add, never override, the production policy",
     ["test_overlay_only_adds_never_overrides", "test_live_test_team_is_dev_only"]),
]

# The gateway image is python:3.12-slim (gateway/Dockerfile); evidence must come from the same minor version.
REQUIRED_PYTHON = (3, 12)


def main() -> int:
    if sys.version_info[:2] != REQUIRED_PYTHON:
        print(f"evidence must run on Python {'.'.join(map(str, REQUIRED_PYTHON))} (the gateway image's), "
              f"not {platform.python_version()}: make venv", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as d:
        junit = Path(d) / "junit.xml"
        code = subprocess.call([sys.executable, "-m", "pytest", "-q", "-m", "not live", f"--junitxml={junit}",
                                "-p", "no:cacheprovider"], cwd=ROOT)
        cases = []
        for tc in ET.parse(junit).getroot().iter("testcase"):
            status = "fail" if tc.find("failure") is not None or tc.find("error") is not None else \
                     "skip" if tc.find("skipped") is not None else "pass"
            cases.append((tc.get("classname", ""), tc.get("name", ""), status))

    valkey = "not available"
    if os.environ.get("REDIS_TEST_URL"):
        import redis
        info = redis.Redis.from_url(os.environ["REDIS_TEST_URL"]).info("server")
        valkey = f"{info.get('server_name', 'valkey')} {info.get('valkey_version') or info.get('redis_version')}"

    total = {s: sum(1 for *_, st in cases if st == s) for s in ("pass", "fail", "skip")}
    lines = [
        "# Test evidence",
        "",
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `scripts/evidence.py` "
        f"(Python {platform.python_version()}, budget store: {valkey}).",
        "",
        f"**{total['pass']} passed, {total['fail']} failed, {total['skip']} skipped.** "
        "Ledger tests run against both the in-memory store and the real Lua scripts on Valkey, "
        "as the least-privilege `slg-gateway` user.",
        "",
        "| Behaviour demonstrated | Tests | Result |",
        "|---|---|---|",
    ]
    claims_ok = True
    for claim, needles in CLAIMS:
        hits = [(n, st) for _, n, st in cases if any(n.startswith(x) for x in needles)]
        ok = bool(hits) and all(st == "pass" for _, st in hits)
        claims_ok &= ok
        names = sorted({n.split("[")[0] for n, _ in hits})
        lines.append(f"| {claim} | {', '.join(f'`{n}`' for n in names) or '-'} ({len(hits)} cases) | "
                     f"{'✅' if ok else '❌'} |")
    if total["skip"]:
        lines += ["", "Skipped:", *[f"- `{c}::{n}`" for c, n, st in cases if st == "skip"]]

    out = ROOT / "docs/evidence/test-evidence.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    print(f"-> {out.relative_to(ROOT)}  ({'all behaviours demonstrated' if claims_ok else 'GAPS'})")
    return code or (0 if claims_ok else 1)


if __name__ == "__main__":
    sys.exit(main())
