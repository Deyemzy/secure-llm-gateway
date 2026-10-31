"""Reservation ledger: lifecycle, idempotency, crash recovery, concurrency, failover.

Every scenario runs against BOTH implementations: InMemoryBudget always, and the real Lua scripts
on Valkey when REDIS_TEST_URL is set (docker compose up -d --wait). CI sets REQUIRE_REDIS=1 so
the Valkey variants fail instead of skipping.

The invariant checked throughout, per team and per user:

    ledger counter == settled actuals + open holds + expired_charged holds   and   counter <= limit
"""

import os
import random
import threading
from datetime import datetime, timezone

import pytest

from app.budget import (BUDGET_TTL_S, HOLD_DEADLINE_S, INDEX_KEY, InMemoryBudget, RedisBudget, keys,
                        request_id_for, safe_id)

TEAM_LIMIT, USER_LIMIT, RPM = 1_000_000, 100_000, 10_000  # $1 team, $0.10 user


class Clock:
    def __init__(self, t=1_793_400_000.0):  # 2026-10-31 UTC
        self.t = t

    def __call__(self):
        return self.t


def redis_urls():
    url = os.environ.get("REDIS_TEST_URL")
    if not url:
        if os.environ.get("REQUIRE_REDIS"):
            pytest.fail("REQUIRE_REDIS is set but REDIS_TEST_URL is not")
        pytest.skip("REDIS_TEST_URL not set (docker compose up -d --wait)")
    return url, os.environ.get("REDIS_TEST_APP_URL", url)


@pytest.fixture
def admin():
    import redis
    url, _ = redis_urls()
    r = redis.Redis.from_url(url)
    for k in r.scan_iter("slg:*"):
        r.delete(k)
    yield r
    for k in r.scan_iter("slg:*"):
        r.delete(k)


@pytest.fixture(params=["memory", "valkey"])
def store(request):
    clock = Clock()
    if request.param == "memory":
        yield InMemoryBudget(clock=clock), clock
        return
    import redis
    admin_ = request.getfixturevalue("admin")
    _, app_url = redis_urls()
    yield RedisBudget(redis.Redis.from_url(app_url), clock=clock), clock


def counter(b, key):
    if isinstance(b, InMemoryBudget):
        return b.data.get(key, 0)
    v = b.r.get(key)
    return int(v) if v is not None else 0


def reserve(b, rid, amount=40_000, user="u", team="t", user_limit=USER_LIMIT, rpm=RPM):
    return b.reserve(team, user, amount, TEAM_LIMIT, user_limit, rpm, rid)


# ---- keys and ids --------------------------------------------------------------------------------

def test_key_format_is_monthly_utc():
    now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    tk, uk, rk, vk = keys("platform", "sub-123", "rid", now)
    assert (tk, uk, vk) == ("slg:budget:team:platform:2026-10", "slg:budget:user:sub-123:2026-10", "slg:resv:rid")
    assert keys("platform", "sub-123", "rid", now + 1)[0].endswith(":2026-11")
    assert 31 * 86400 < BUDGET_TTL_S <= 40 * 86400


@pytest.mark.parametrize("raw", ["a:b", "x y", "{tag}", "a" * 200, "", "ü"])
def test_unsafe_ids_are_hashed(raw):
    out = safe_id(raw)
    assert out.startswith("h-") and ":" not in out and " " not in out


def test_idempotency_ids_are_scoped_per_user():
    assert request_id_for("alice", "key-12345") == request_id_for("alice", "key-12345")
    assert request_id_for("alice", "key-12345") != request_id_for("bob", "key-12345")
    assert request_id_for("alice", None) != request_id_for("alice", None)
    # no delimiter games: ("ab", "c...") and ("a", "bc...") must not collide
    assert request_id_for("ab", "c-key-123") != request_id_for("a", "bc-key-123")


def test_cross_user_same_key_holds_independently(store):
    """Same Idempotency-Key, two users: two independent holds on the real Lua too."""
    b, _ = store
    a = b.reserve("t", "sub-alice", 40_000, TEAM_LIMIT, USER_LIMIT, RPM, request_id_for("sub-alice", "k-shared-01"))
    c = b.reserve("t", "sub-bob", 40_000, TEAM_LIMIT, USER_LIMIT, RPM, request_id_for("sub-bob", "k-shared-01"))
    assert a.ok and c.ok and a.reservation.resv_key != c.reservation.resv_key


# ---- lifecycle (both stores) ------------------------------------------------------------------------

def test_reserve_begin_settle(store):
    b, _ = store
    r = reserve(b, "r1")
    assert r.ok and r.user_spent == 40_000
    assert b.begin(r.reservation)
    s = b.settle(r.reservation, 1_000, "settled")
    assert (s.applied, s.delta, s.user_spent, s.team_spent) == (True, -39_000, 1_000, 1_000)
    assert b.hold_state("r1") == "settled"


def test_settle_is_idempotent(store):
    b, _ = store
    r = reserve(b, "r1").reservation
    b.begin(r)
    b.settle(r, 1_000, "settled")
    again = b.settle(r, 1_000, "settled")
    assert not again.applied and again.user_spent == 1_000


def test_release_before_model_call(store):
    b, _ = store
    r = reserve(b, "r1").reservation
    s = b.settle(r, 0, "released")
    assert s.user_spent == 0 and b.hold_state("r1") == "released"


def test_duplicate_in_flight_and_completed(store):
    b, _ = store
    r = reserve(b, "ik-1").reservation
    assert reserve(b, "ik-1").reason == "duplicate_in_flight"
    b.begin(r)
    assert reserve(b, "ik-1").reason == "duplicate_in_flight"
    b.settle(r, 1_000, "settled")
    dup = reserve(b, "ik-1")
    assert dup.reason == "duplicate_completed" and dup.user_spent == 1_000   # not charged twice


def test_limits_and_rate(store):
    b, _ = store
    assert reserve(b, "a1", 100_000, user="alice").ok
    assert reserve(b, "a2", 1, user="alice").reason == "user_budget_exceeded"
    assert b.reserve("t", "bob", 100_000, 150_000, USER_LIMIT, RPM, "b1").reason == "team_budget_exceeded"
    for i in range(3):
        assert reserve(b, f"c{i}", 1, user="carol", rpm=3).ok
    assert reserve(b, "c9", 1, user="carol", rpm=3).reason == "rate_limited"


def test_begin_refused_after_deadline(store):
    b, clock = store
    r = reserve(b, "r1").reservation
    clock.t += HOLD_DEADLINE_S + 1
    assert not b.begin(r)   # never call Bedrock on a hold the reconciler may release


# ---- crash recovery (both stores) -----------------------------------------------------------------

def test_crash_before_model_call_is_refunded(store):
    b, clock = store
    r = reserve(b, "crash-early").reservation             # worker dies here
    assert b.reconcile() == []                             # not due yet
    clock.t += HOLD_DEADLINE_S + 1
    [o] = b.reconcile()
    assert (o.request_id, o.outcome, o.amount) == ("crash-early", "expired_released", 40_000)
    assert counter(b, r.user_key) == 0 and b.hold_state("crash-early") == "expired_released"
    assert b.reconcile() == []                             # idempotent


def test_crash_during_model_call_stays_charged(store):
    b, clock = store
    r = reserve(b, "crash-late").reservation
    b.begin(r)                                             # Bedrock may have billed; worker dies
    clock.t += HOLD_DEADLINE_S + 1
    [o] = b.reconcile()
    assert o.outcome == "expired_charged" and counter(b, r.user_key) == 40_000   # never under-count


def test_late_settle_corrects_expired_charged(store):
    b, clock = store
    r = reserve(b, "slow").reservation
    b.begin(r)
    clock.t += HOLD_DEADLINE_S + 1
    b.reconcile()
    s = b.settle(r, 2_000, "settled")                     # the slow request finally finished
    assert s.applied and s.user_spent == 2_000 and b.hold_state("slow") == "settled"


def test_reconcile_never_touches_closed_holds(store):
    b, clock = store
    r = reserve(b, "done").reservation
    b.begin(r)
    b.settle(r, 1_000, "settled")
    clock.t += HOLD_DEADLINE_S + 1
    assert all(o.outcome != "expired_charged" for o in b.reconcile())
    assert counter(b, r.user_key) == 1_000


# ---- the invariant under random crashes (both stores) -------------------------------------------------

@pytest.mark.parametrize("seed", range(5))
def test_invariant_under_random_crashes_and_retries(store, seed):
    """Random mix of: normal requests, crashes before/after the model call, model failures,
    duplicate retries, late settles and reconciler runs, with random actual costs. After every
    step: counter == settled actuals + open/expired_charged holds, and counter <= limit."""
    b, clock = store
    rng = random.Random(seed)
    fate = {}            # rid -> ("open"|"charged", amount) or ("closed", actual)
    holds = {}

    def check():
        expected = sum(v for kind, v in fate.values())
        tk = keys("t", "u", "x", clock())[0]
        uk = keys("t", "u", "x", clock())[1]
        assert counter(b, uk) == counter(b, tk) == expected
        assert expected <= USER_LIMIT

    for i in range(60):
        op = rng.choice(["ok", "ok", "crash_early", "crash_late", "model_fail", "dup", "reconcile", "tick"])
        rid = f"r{i}"
        amount = rng.randint(1_000, 30_000)
        if op in ("ok", "crash_early", "crash_late", "model_fail"):
            res = reserve(b, rid, amount)
            if not res.ok:
                assert res.reason == "user_budget_exceeded"
                continue
            fate[rid] = ("open", amount)
            holds[rid] = res.reservation
            if op == "crash_early":
                pass
            elif b.begin(res.reservation):
                if op == "ok":
                    actual = rng.randint(0, amount)
                    b.settle(res.reservation, actual, "settled")
                    fate[rid] = ("closed", actual)
                elif op == "model_fail":
                    b.settle(res.reservation, 0, "released")
                    fate[rid] = ("closed", 0)
                # crash_late: begun, never settled
        elif op == "dup" and holds:
            old = rng.choice(list(holds))
            assert not reserve(b, old, amount).ok          # a retry never holds budget twice
        elif op == "reconcile":
            for o in b.reconcile():
                kind, v = fate[o.request_id]
                fate[o.request_id] = ("closed", 0) if o.outcome == "expired_released" else ("charged", v)
        elif op == "tick":
            clock.t += rng.choice([10, 120, HOLD_DEADLINE_S + 1])
        check()

    clock.t += HOLD_DEADLINE_S + 1                          # everything left open eventually resolves
    for o in b.reconcile(limit=1000):
        kind, v = fate[o.request_id]
        fate[o.request_id] = ("closed", 0) if o.outcome == "expired_released" else ("charged", v)
    check()
    assert all(kind != "open" for kind, _ in fate.values())


# ---- rebuild floor (both stores) --------------------------------------------------------------------

def test_raise_floor_only_raises(store):
    b, _ = store
    r = reserve(b, "r1", 40_000).reservation
    assert b.raise_floor(r.user_key, 10_000) == 0 and counter(b, r.user_key) == 40_000   # never lowers
    assert b.raise_floor(r.user_key, 70_000) == 30_000 and counter(b, r.user_key) == 70_000
    assert b.raise_floor("slg:budget:user:lost:2026-10", 5_000) == 5_000                 # missing key


# ---- concurrency ------------------------------------------------------------------------------------

def test_concurrent_requests_cannot_overspend(store):
    """$0.10 left, ten simultaneous $0.04 holds: exactly two get through, on either store."""
    from concurrent.futures import ThreadPoolExecutor
    b, _ = store
    barrier = threading.Barrier(10)

    def go(i):
        barrier.wait()
        return reserve(b, f"race-{i}")

    with ThreadPoolExecutor(10) as ex:
        results = list(ex.map(go, range(10)))
    assert sum(r.ok for r in results) == 2
    assert all(r.reason == "user_budget_exceeded" for r in results if not r.ok)
    uk = next(r.reservation.user_key for r in results if r.ok)
    assert counter(b, uk) == 80_000 <= USER_LIMIT


# ---- Valkey only: durability, replication, failover, ACL ---------------------------------------------

def test_valkey_ttls(admin):
    import redis
    _, app_url = redis_urls()
    b = RedisBudget(redis.Redis.from_url(app_url))
    r = reserve(b, "ttl").reservation
    b.begin(r)
    b.settle(r, 1_000, "settled")
    assert 34 * 86400 < admin.ttl(r.user_key) <= BUDGET_TTL_S
    assert 6 * 86400 < admin.ttl(r.resv_key) <= 7 * 86400          # idempotency record kept a week
    assert admin.zscore(INDEX_KEY, "ttl") is None                   # closed holds leave the index


FAILOVER_WAIT_MS = 2_000  # these tests prove data survives failover, not the 200 ms production timeout


def replica(admin):
    """Replica client, guaranteed in sync: the primary must get a real WAIT acknowledgement from it.
    Called at the start of every replica test, so no test depends on an earlier one's cleanup."""
    import redis
    url = os.environ.get("REDIS_TEST_REPLICA_URL")
    if not url:
        if os.environ.get("REQUIRE_REDIS"):
            pytest.fail("REQUIRE_REDIS is set but REDIS_TEST_REPLICA_URL is not")
        pytest.skip("REDIS_TEST_REPLICA_URL not set")
    rep = redis.Redis.from_url(url)
    _wait_in_sync(admin)
    return rep


def _wait_in_sync(admin, deadline_s: float = 20.0):
    import time
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        admin.set("slg:sync-probe", "1")
        if int(admin.execute_command("WAIT", 1, 500)) >= 1:
            admin.delete("slg:sync-probe")
            return
        time.sleep(0.2)
    pytest.fail("replica never acknowledged a write (not attached / not in sync)")


def _reattach(rep, admin):
    """Undo an injected failover: make the replica follow the primary again, fully resynced."""
    rep.execute_command("REPLICAOF", "valkey", "6379")
    _wait_in_sync(admin)


def test_valkey_replica_ack_gate(admin):
    """With min_replica_acks=1 the hold is confirmed on the replica before the model call; asking
    for more replicas than exist must report not-durable (the gateway then fails closed: 503)."""
    import redis
    rep = replica(admin)
    _, app_url = redis_urls()
    b = RedisBudget(redis.Redis.from_url(app_url), min_replica_acks=1, wait_timeout_ms=FAILOVER_WAIT_MS)
    r = reserve(b, "durable").reservation
    assert b.begin(r) and b.wait_replicated()
    assert rep.hget(r.resv_key, "state") == b"invoking" and int(rep.get(r.user_key)) == 40_000
    impossible = RedisBudget(redis.Redis.from_url(app_url), min_replica_acks=2, wait_timeout_ms=100)
    assert impossible.wait_replicated() is False


def test_valkey_crash_after_durable_preinvoke_write_then_failover(admin):
    """Real Valkey: reserve -> begin(invoking) -> WAIT acked by the replica -> worker dies before the
    Bedrock call -> primary fails over. The promoted replica holds the 'invoking' record, and the
    reconciler running against it keeps the full hold charged (never refunds an unknown outcome)."""
    import redis
    rep_admin = replica(admin)
    _, app_url = redis_urls()
    clock = Clock()
    b = RedisBudget(redis.Redis.from_url(app_url), clock=clock, min_replica_acks=1,
                    wait_timeout_ms=FAILOVER_WAIT_MS)
    r = reserve(b, "durable-crash", 40_000).reservation
    assert b.begin(r) and b.wait_replicated()
    # --- worker dies here: no settle, no Bedrock call ---
    rep_url = os.environ["REDIS_TEST_REPLICA_URL"].replace("slg-test:test-only", "slg-gateway:local-dev-only")
    try:
        rep_admin.execute_command("REPLICAOF", "NO", "ONE")                           # failover
        promoted = RedisBudget(redis.Redis.from_url(rep_url), clock=clock)
        assert promoted.hold_state("durable-crash") == "invoking"
        clock.t += HOLD_DEADLINE_S + 1
        [o] = [x for x in promoted.reconcile() if x.request_id == "durable-crash"]
        assert o.outcome == "expired_charged" and int(rep_admin.get(r.user_key)) == 40_000
    finally:
        _reattach(rep_admin, admin)


def test_valkey_wait_timeout_fails_closed_end_to_end(admin, verifier):
    """The full app on real Valkey, requiring more replica acks than exist: the real WAIT times out,
    the request gets 503, Bedrock is never called, and the hold is released (ledger back to zero)."""
    import io
    import json as _json

    import redis
    from fastapi.testclient import TestClient

    from app.bedrock import Bedrock
    from app.logging import AuditLog
    from app.main import Deps, create_app
    from app.policy import PolicyStore, file_source
    from conftest import POLICY_PATH, FakeConverse, auth, chat_body
    replica(admin)
    _, app_url = redis_urls()
    fake, logs = FakeConverse(), io.StringIO()
    budget = RedisBudget(redis.Redis.from_url(app_url), min_replica_acks=2, wait_timeout_ms=100)
    deps = Deps(verifier, PolicyStore(file_source(str(POLICY_PATH))), budget, Bedrock(fake, "gr", "1"),
                AuditLog(logs))
    r = TestClient(create_app(deps)).post("/v1/chat/completions", json=chat_body(), headers=auth(user="walt"))
    assert r.status_code == 503 and r.json()["error"]["code"] == "dependency_unavailable"
    assert fake.calls == []
    line = _json.loads(logs.getvalue().splitlines()[-1])
    assert line["detail"] == "replica_ack_timeout"
    assert budget.hold_state(line["ledger_id"]) == "released"
    assert all(int(admin.get(k) or 0) == 0 for k in admin.scan_iter("slg:budget:user:sub-walt:*"))


def test_valkey_failover_keeps_acknowledged_holds(admin):
    """Failure injection: promote the replica (as a failover would) right after a WAIT-acknowledged
    reservation. The new primary must have the hold and the counters: no under-count."""
    import redis
    rep = replica(admin)
    _, app_url = redis_urls()
    b = RedisBudget(redis.Redis.from_url(app_url), min_replica_acks=1, wait_timeout_ms=FAILOVER_WAIT_MS)
    results = [reserve(b, f"fo-{i}", 10_000) for i in range(5)]
    for r in results:
        assert r.ok and b.begin(r.reservation) and b.wait_replicated()
    uk = results[0].reservation.user_key
    primary_count = int(admin.get(uk))
    try:
        rep.execute_command("REPLICAOF", "NO", "ONE")                 # failover
        assert int(rep.get(uk)) == primary_count == 50_000
        assert all(rep.hget(r.reservation.resv_key, "state") == b"invoking" for r in results)
        assert rep.zcard(INDEX_KEY) >= 5                              # reconciler can still find them
    finally:
        _reattach(rep, admin)


@pytest.mark.parametrize("cmd", [("FLUSHALL",), ("KEYS", "*"), ("CONFIG", "GET", "*"), ("SET", "other:key", "1"),
                                 ("GET", "other:key"), ("FLUSHDB",), ("SCRIPT", "FLUSH"),
                                 ("REPLICAOF", "NO", "ONE"), ("ACL", "LIST"), ("SCAN", "0")])
def test_valkey_gateway_user_is_least_privilege(admin, cmd):
    import redis
    _, app_url = redis_urls()
    if app_url == os.environ.get("REDIS_TEST_URL"):
        pytest.skip("REDIS_TEST_APP_URL not set to the slg-gateway user")
    with pytest.raises(redis.exceptions.NoPermissionError):
        redis.Redis.from_url(app_url).execute_command(*cmd)


def test_acl_matches_terraform():
    """The compose ACL (exercised in CI) and the Terraform ACL (deployed) grant the same commands."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    tf = (root / "terraform/modules/gateway/main.tf").read_text()
    block = tf[tf.index("redis_acl = join"):]
    block = block[:block.index("])")]
    acl = (root / "deploy/valkey/users.acl").read_text()
    line = next(l for l in acl.splitlines() if l.startswith("user slg-gateway "))
    assert set(re.findall(r"\+[\w|]+", block)) == set(re.findall(r"\+[\w|]+", line))
    assert "~slg:*" in block and "~slg:*" in line
