"""Reservation ledger for monthly spend.

Invariant, per team and per user, for every month:

    settled spend + outstanding holds  ==  ledger counter  <=  limit

Lifecycle of one request's hold (a hash at slg:resv:<request id>):

    reserved ──begin──▶ invoking ──settle──▶ settled          (actual cost)
       │                   │      └─settle──▶ released         (model refused/failed, unbilled)
       │                   └──reconcile (deadline passed)──▶ expired_charged  (keep worst case)
       ├──settle──▶ released                                    (failed before the model call)
       └──reconcile (deadline passed)──▶ expired_released       (crashed before the call: refund)

- Reserve is atomic (Lua) across team + user counters, so concurrent requests can't overspend.
- Request ids derived from a client Idempotency-Key make retries unable to hold budget twice.
- begin() must succeed before Bedrock is called, so a reconciled hold can't be spent against.
- wait_replicated() blocks until the hold is on N replicas, so a failover can't silently lose an
  acknowledged reservation (ElastiCache replication is asynchronous).
- reconcile() recovers holds whose worker died, without ever under-counting.

Keys (UTC calendar month):
    slg:budget:team:<team>:<YYYY-MM>    slg:budget:user:<sub>:<YYYY-MM>    (TTL ~35 days)
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

HERE = Path(__file__).parent
RESERVE_LUA = (HERE / "reserve.lua").read_text()
BEGIN_LUA = (HERE / "begin.lua").read_text()
SETTLE_LUA = (HERE / "settle.lua").read_text()
RECONCILE_LUA = (HERE / "reconcile.lua").read_text()
RAISE_LUA = (HERE / "raise.lua").read_text()

BUDGET_TTL_S = 35 * 86400
HOLD_DEADLINE_S = 300            # > Bedrock read timeout x retries; after this a hold is reconciled
CLOSED_RECORD_TTL_S = 7 * 86400  # idempotency window for completed requests
CHARGED_RECORD_TTL_S = BUDGET_TTL_S  # expired_charged records stay for manual reconciliation
INDEX_KEY = "slg:resv:deadlines"
SAFE_ID = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

OPEN_STATES = ("reserved", "invoking")


class BudgetUnavailable(Exception):
    """The ledger could not be reached or could not confirm durability. Callers must fail closed."""


@dataclass(frozen=True)
class Reservation:
    id: str
    team_key: str
    user_key: str
    resv_key: str
    amount: int


@dataclass(frozen=True)
class ReserveResult:
    ok: bool
    reason: str
    team_spent: int
    user_spent: int
    reservation: Reservation | None = None


@dataclass(frozen=True)
class SettleResult:
    applied: bool
    delta: int
    team_spent: int
    user_spent: int


@dataclass(frozen=True)
class ReconcileOutcome:
    request_id: str
    outcome: str     # expired_released | expired_charged | closed | gone | not_due
    amount: int
    team: str = ""
    user: str = ""


class BudgetStore(Protocol):
    def reserve(self, team: str, user: str, amount: int, team_limit: int, user_limit: int, rpm: int,
                request_id: str) -> ReserveResult: ...
    def begin(self, r: Reservation) -> bool: ...
    def wait_replicated(self) -> bool: ...
    def settle(self, r: Reservation, actual: int, state: str = "settled") -> SettleResult: ...
    def reconcile(self, limit: int = 100) -> list[ReconcileOutcome]: ...
    def raise_floor(self, key: str, floor: int) -> int: ...


def budget_key(scope: str, ident: str, month: str) -> str:
    return f"slg:budget:{scope}:{safe_id(ident)}:{month}"


def safe_id(value: str) -> str:
    """Ids go into key names verbatim when they're plain; anything else is hashed."""
    return value if SAFE_ID.match(value) else "h-" + hashlib.sha256(value.encode()).hexdigest()[:32]


def request_id_for(user: str, idempotency_key: str | None) -> str:
    """Scoped per user, so one user's key can never collide with (or probe) another user's."""
    if idempotency_key is None:
        return uuid.uuid4().hex
    return "ik-" + hashlib.sha256(f"{user}\0{idempotency_key}".encode()).hexdigest()[:32]


def keys(team: str, user: str, rid: str, now: float) -> tuple[str, str, str, str]:
    month = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")
    t, u = safe_id(team), safe_id(user)
    return (f"slg:budget:team:{t}:{month}",
            f"slg:budget:user:{u}:{month}",
            f"slg:rpm:user:{u}:{int(now // 60)}",
            f"slg:resv:{rid}")


def _s(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v)


class RedisBudget:
    def __init__(self, client, clock=time.time, min_replica_acks: int = 0, wait_timeout_ms: int = 200):
        self.r = client
        self.clock = clock
        self.min_replica_acks = min_replica_acks
        self.wait_timeout_ms = wait_timeout_ms
        self._reserve = client.register_script(RESERVE_LUA)
        self._begin = client.register_script(BEGIN_LUA)
        self._settle = client.register_script(SETTLE_LUA)
        self._reconcile = client.register_script(RECONCILE_LUA)
        self._raise = client.register_script(RAISE_LUA)

    def _call(self, fn, **kw):
        try:
            return fn(**kw)
        except Exception as e:
            raise BudgetUnavailable(type(e).__name__) from e

    def reserve(self, team, user, amount, team_limit, user_limit, rpm, request_id) -> ReserveResult:
        now = self.clock()
        tk, uk, rk, vk = keys(team, user, request_id, now)
        ok, reason, ts, us = self._call(
            self._reserve, keys=[tk, uk, rk, vk, INDEX_KEY],
            args=[amount, team_limit, user_limit, rpm, BUDGET_TTL_S, int(now), int(now + HOLD_DEADLINE_S),
                  request_id, team, user])
        res = Reservation(request_id, tk, uk, vk, amount) if ok else None
        return ReserveResult(bool(ok), _s(reason), int(ts), int(us), res)

    def begin(self, r: Reservation) -> bool:
        return bool(self._call(self._begin, keys=[r.resv_key], args=[int(self.clock())]))

    def wait_replicated(self) -> bool:
        if self.min_replica_acks <= 0:
            return True
        try:
            acked = self.r.wait(self.min_replica_acks, self.wait_timeout_ms)
        except Exception as e:
            raise BudgetUnavailable(type(e).__name__) from e
        return int(acked) >= self.min_replica_acks

    def settle(self, r: Reservation, actual: int, state: str = "settled") -> SettleResult:
        applied, delta, ts, us = self._call(
            self._settle, keys=[r.team_key, r.user_key, r.resv_key, INDEX_KEY],
            args=[actual, state, int(self.clock()), r.id, CLOSED_RECORD_TTL_S])
        return SettleResult(bool(applied), int(delta), int(ts), int(us))

    def reconcile(self, limit: int = 100) -> list[ReconcileOutcome]:
        now = int(self.clock())
        try:
            due = self.r.zrangebyscore(INDEX_KEY, "-inf", now, start=0, num=limit)
        except Exception as e:
            raise BudgetUnavailable(type(e).__name__) from e
        out = []
        for raw in due:
            rid = _s(raw)
            vk = f"slg:resv:{rid}"
            fields = self.r.hmget(vk, "team_key", "user_key", "team", "user")
            tk, uk, team, user = (_s(f) if f is not None else "" for f in fields)
            outcome, amount = self._call(
                self._reconcile, keys=[tk or "slg:none", uk or "slg:none", vk, INDEX_KEY],
                args=[now, rid, CLOSED_RECORD_TTL_S, CHARGED_RECORD_TTL_S])
            out.append(ReconcileOutcome(rid, _s(outcome), int(amount), team, user))
        return out

    def hold_state(self, request_id: str) -> str | None:
        v = self.r.hget(f"slg:resv:{request_id}", "state")
        return _s(v) if v is not None else None

    def raise_floor(self, key: str, floor: int) -> int:
        return int(self._call(self._raise, keys=[key], args=[floor, BUDGET_TTL_S]))


class InMemoryBudget:
    """Same state machine as the Lua scripts. Local runs and app tests only (ENVIRONMENT=local).
    tests/unit/test_ledger.py runs one scenario suite against both implementations."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.data: dict[str, int] = {}
        self.holds: dict[str, dict] = {}
        self.lock = threading.Lock()

    @property
    def resv(self) -> dict[str, int]:  # open holds, for test assertions
        return {k: h["amount"] for k, h in self.holds.items() if h["state"] in OPEN_STATES}

    def reserve(self, team, user, amount, team_limit, user_limit, rpm, request_id) -> ReserveResult:
        now = self.clock()
        tk, uk, rk, vk = keys(team, user, request_id, now)
        with self.lock:
            ts, us = self.data.get(tk, 0), self.data.get(uk, 0)
            if vk in self.holds:
                dup = "duplicate_in_flight" if self.holds[vk]["state"] in OPEN_STATES else "duplicate_completed"
                return ReserveResult(False, dup, ts, us)
            self.data[rk] = self.data.get(rk, 0) + 1
            if self.data[rk] > rpm:
                return ReserveResult(False, "rate_limited", ts, us)
            if ts + amount > team_limit:
                return ReserveResult(False, "team_budget_exceeded", ts, us)
            if us + amount > user_limit:
                return ReserveResult(False, "user_budget_exceeded", ts, us)
            self.data[tk] = ts + amount
            self.data[uk] = us + amount
            self.holds[vk] = {"state": "reserved", "amount": amount, "team_key": tk, "user_key": uk,
                              "deadline": int(now + HOLD_DEADLINE_S), "id": request_id, "team": team, "user": user}
            return ReserveResult(True, "ok", ts + amount, us + amount, Reservation(request_id, tk, uk, vk, amount))

    def begin(self, r: Reservation) -> bool:
        with self.lock:
            h = self.holds.get(r.resv_key)
            if not h or h["state"] != "reserved" or h["deadline"] <= int(self.clock()):
                return False
            h["state"] = "invoking"
            return True

    def wait_replicated(self) -> bool:
        return True

    def settle(self, r: Reservation, actual: int, state: str = "settled") -> SettleResult:
        with self.lock:
            h = self.holds.get(r.resv_key)
            delta, applied = 0, False
            if h and h["state"] in OPEN_STATES + ("expired_charged",):
                delta = actual - h["amount"]
                self.data[r.team_key] += delta
                self.data[r.user_key] += delta
                h.update(state=state, actual=actual)
                applied = True
            return SettleResult(applied, delta, self.data.get(r.team_key, 0), self.data.get(r.user_key, 0))

    def reconcile(self, limit: int = 100) -> list[ReconcileOutcome]:
        now = int(self.clock())
        out = []
        with self.lock:
            for vk, h in list(self.holds.items()):
                if len(out) >= limit or h["state"] not in OPEN_STATES or h["deadline"] > now:
                    continue
                if h["state"] == "reserved":
                    self.data[h["team_key"]] -= h["amount"]
                    self.data[h["user_key"]] -= h["amount"]
                    h.update(state="expired_released", actual=0)
                else:
                    h["state"] = "expired_charged"
                out.append(ReconcileOutcome(h["id"], h["state"], h["amount"], h["team"], h["user"]))
        return out

    def raise_floor(self, key: str, floor: int) -> int:
        with self.lock:
            cur = self.data.get(key, 0)
            if floor <= cur:
                return 0
            self.data[key] = floor
            return floor - cur

    def hold_state(self, request_id: str) -> str | None:
        for h in self.holds.values():
            if h["id"] == request_id:
                return h["state"]
        return None
