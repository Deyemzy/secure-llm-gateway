"""Rebuild the budget ledger from the audit logs, in the safe direction only.

If Valkey loses data (a node replaced without a replica, a bad restore, an accidental flush), every
counter would restart at zero spent: an under-count, the one failure that allows overspend. This
job sums the month's spend from the metadata-only audit logs and **raises** any team or user
counter that is lower. It never lowers a counter, because the ledger also holds open reservations
the logs don't know about yet.

    python -m app.rebuild            (EventBridge Scheduler runs it hourly as an ECS task)

Spend counted from the logs, per (team, sub), for the current UTC month:
  - request events with cost_usd > 0  (settled actuals, guardrail blocks, charged unknown outcomes)
  - reconcile events with outcome expired_charged (holds kept at worst case after a crash)
A request settled late after an expired_charged hold is counted twice, which over-counts. That's
the safe direction.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from .budget import BudgetStore, budget_key
from .logging import AuditLog

QUERY = """
filter (event = "request" and cost_usd > 0) or (event = "reconcile" and outcome = "expired_charged")
| fields coalesce(cost_usd, reserved_usd) as usd
| stats sum(usd) as spent by team, sub
| limit 10000
"""


def month_start(now: float) -> tuple[str, int]:
    d = datetime.fromtimestamp(now, timezone.utc)
    start = datetime(d.year, d.month, 1, tzinfo=timezone.utc)
    return d.strftime("%Y-%m"), int(start.timestamp())


def logged_spend(logs, log_group: str, now: float, poll_s: float = 1.0,
                 timeout_s: float = 300) -> list[tuple[str, str, int]]:
    """[(team, sub, micro_usd)] for the current month, from CloudWatch Logs Insights."""
    _, start = month_start(now)
    qid = logs.start_query(logGroupName=log_group, startTime=start, endTime=int(now), queryString=QUERY)["queryId"]
    deadline = time.monotonic() + timeout_s
    while True:
        r = logs.get_query_results(queryId=qid)
        if r["status"] == "Complete":
            break
        if r["status"] in ("Failed", "Cancelled", "Timeout", "Unknown") or time.monotonic() > deadline:
            raise RuntimeError(f"logs query {r['status']}")
        time.sleep(poll_s)
    rows = []
    for row in r["results"]:
        f = {c["field"]: c["value"] for c in row}
        if f.get("team") and f.get("sub") and f.get("spent"):
            rows.append((f["team"], f["sub"], round(float(f["spent"]) * 1e6)))
    if len(rows) >= 10_000:
        raise RuntimeError("logs query hit the 10,000-row limit; shard the query by team")
    return rows


def rebuild(budget: BudgetStore, rows: list[tuple[str, str, int]], now: float, audit: AuditLog) -> dict:
    month, _ = month_start(now)
    team_totals: dict[str, int] = defaultdict(int)
    user_totals: dict[str, int] = defaultdict(int)
    for team, sub, micro in rows:
        team_totals[team] += micro
        user_totals[sub] += micro

    raised = []
    for scope, totals in (("team", team_totals), ("user", user_totals)):
        for ident, micro in totals.items():
            added = budget.raise_floor(budget_key(scope, ident, month), micro)
            if added:
                raised.append({"scope": scope, "id": ident, "added_usd": added / 1e6})
    summary = {"month": month, "teams": len(team_totals), "users": len(user_totals), "raised": len(raised)}
    audit.event("ledger_rebuild", outcome="raised" if raised else "consistent", detail=json.dumps(summary))
    for r in raised:  # each one means the ledger had lost spend
        audit.event("ledger_rebuild_raise", team=r["id"] if r["scope"] == "team" else None,
                    sub=r["id"] if r["scope"] == "user" else None, cost_usd=r["added_usd"])
    return {**summary, "details": raised}


def connect(env=os.environ):
    """(redis client, CloudWatch Logs client) from the gateway task's environment."""
    import boto3
    import redis
    client = redis.Redis(host=env["REDIS_HOST"], port=int(env.get("REDIS_PORT", "6379")),
                         username=env.get("REDIS_USER") or None, password=env.get("REDIS_PASSWORD") or None,
                         ssl=env.get("REDIS_TLS", "true").lower() == "true", socket_timeout=5)
    return client, boto3.client("logs", region_name=env.get("AWS_REGION", "us-east-1"))


def main() -> int:
    from .budget import RedisBudget
    env = os.environ
    client, logs = connect(env)
    now = time.time()
    rows = logged_spend(logs, env["GATEWAY_LOG_GROUP"], now)
    result = rebuild(RedisBudget(client), rows, now, AuditLog())
    print(json.dumps({k: v for k, v in result.items() if k != "details"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
