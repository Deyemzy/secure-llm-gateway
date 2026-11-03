"""Ledger rebuild from audit logs: restores lost spend, never lowers the ledger."""

import json
import time

import pytest

from app.rebuild import QUERY, logged_spend, month_start, rebuild
from conftest import chat_body, auth

NOW = 1_793_400_000.0  # 2026-10-31 UTC


def insights(logs: list[dict]) -> list[tuple[str, str, int]]:
    """What QUERY computes, evaluated over the gateway's own log records (for tests)."""
    totals: dict[tuple[str, str], float] = {}
    for r in logs:
        if (r.get("event") == "request" and (r.get("cost_usd") or 0) > 0) or \
           (r.get("event") == "reconcile" and r.get("outcome") == "expired_charged"):
            usd = r.get("cost_usd") if r.get("cost_usd") is not None else r.get("reserved_usd")
            key = (r["team"], r["sub"])
            totals[key] = totals.get(key, 0) + usd
    return [(t, s, round(v * 1e6)) for (t, s), v in totals.items()]


class Audit:
    def __init__(self):
        self.events = []

    def event(self, name, **f):
        self.events.append({"event": name, **f})


def test_disaster_drill_valkey_loses_everything(stack):
    """Traffic -> ledger wiped -> rebuild from logs -> budgets enforced again, not reset to zero."""
    for user in ("alice", "bob", "alice"):
        assert stack.client.post("/v1/chat/completions", json=chat_body(), headers=auth(user=user)).status_code == 200
    before = {k: v for k, v in stack.budget.data.items() if k.startswith("slg:budget:")}
    assert before

    stack.budget.data.clear()                                 # node replaced without a replica
    rows = insights(stack.logs())
    result = rebuild(stack.budget, rows, time.time(), Audit())   # same month as the traffic

    after = {k: v for k, v in stack.budget.data.items() if k.startswith("slg:budget:")}
    assert after == before                                    # every team and user counter restored
    assert result["raised"] == len(before)


def test_rebuild_restores_enforcement(stack):
    """An over-budget user must stay over budget after the ledger is lost and rebuilt."""
    from app.budget import budget_key
    assert stack.client.post("/v1/chat/completions", json=chat_body(),
                             headers=auth(user="carol", team="research")).status_code == 200
    month, _ = month_start(__import__("time").time())
    # pretend carol had spent her whole $5 this month (as the logs would show)
    logs = stack.logs() + [{"event": "request", "team": "research", "sub": "sub-carol", "cost_usd": 5.0}]
    stack.budget.data.clear()
    rebuild(stack.budget, insights(logs), __import__("time").time(), Audit())
    assert stack.budget.data[budget_key("user", "sub-carol", month)] >= 5_000_000
    r = stack.client.post("/v1/chat/completions", json=chat_body(), headers=auth(user="carol", team="research"))
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded"


def test_rebuild_never_lowers_the_ledger(stack):
    from app.budget import budget_key
    month, _ = month_start(NOW)
    k = budget_key("user", "sub-dan", month)
    stack.budget.data[k] = 900_000                           # includes open holds the logs can't see
    audit = Audit()
    rebuild(stack.budget, [("platform", "sub-dan", 400_000)], NOW, audit)
    assert stack.budget.data[k] == 900_000
    assert json.loads(audit.events[0]["detail"])["raised"] == 1   # only the (missing) team counter


def test_rebuild_counts_expired_charged_and_unknown_outcomes():
    logs = [
        {"event": "request", "team": "t", "sub": "s", "cost_usd": 0.01},
        {"event": "request", "team": "t", "sub": "s", "cost_usd": 0.05, "outcome_unknown": True},
        {"event": "reconcile", "team": "t", "sub": "s", "outcome": "expired_charged", "reserved_usd": 0.02},
        {"event": "reconcile", "team": "t", "sub": "s", "outcome": "expired_released", "reserved_usd": 9.0},
        {"event": "request", "team": "t", "sub": "s", "code": "unauthorized"},
    ]
    assert insights(logs) == [("t", "s", 80_000)]


def test_query_covers_both_event_kinds():
    assert 'event = "request" and cost_usd > 0' in QUERY
    assert 'outcome = "expired_charged"' in QUERY and "coalesce(cost_usd, reserved_usd)" in QUERY


class FakeLogs:
    def __init__(self, results, statuses=("Running", "Complete")):
        self.statuses = list(statuses)
        self.results = results
        self.started = None

    def start_query(self, **kw):
        self.started = kw
        return {"queryId": "q1"}

    def get_query_results(self, queryId):
        status = self.statuses.pop(0) if self.statuses else "Complete"
        return {"status": status, "results": self.results if status == "Complete" else []}


def test_logged_spend_parses_insights_and_scopes_to_month():
    rows = [[{"field": "team", "value": "platform"}, {"field": "sub", "value": "sub-a"},
             {"field": "spent", "value": "1.25"}]]
    logs = FakeLogs(rows)
    out = logged_spend(logs, "/slg-dev/gateway", NOW, poll_s=0)
    assert out == [("platform", "sub-a", 1_250_000)]
    _, start = month_start(NOW)
    assert logs.started["startTime"] == start and logs.started["logGroupName"] == "/slg-dev/gateway"


def test_logged_spend_fails_loudly():
    with pytest.raises(RuntimeError, match="Failed"):
        logged_spend(FakeLogs([], statuses=("Failed",)), "g", NOW, poll_s=0)
    row = [{"field": "team", "value": "t"}, {"field": "sub", "value": "s"}, {"field": "spent", "value": "1"}]
    with pytest.raises(RuntimeError, match="10,000"):
        logged_spend(FakeLogs([row] * 10_000), "g", NOW, poll_s=0)
