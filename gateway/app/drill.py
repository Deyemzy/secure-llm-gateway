"""Ledger-rebuild drill: prove the hourly rebuild restores lost spend, on a real deployment.

    python -m app.drill --team live-test      (make drill-ledger-rebuild runs it as an ECS task)

For one test team, as the dev-only Valkey user slg-drill (the gateway's own user has no delete
commands; terraform enable_ledger_drill creates this user, its task definition and roles):
  1. preflight: every counter of the team and its users must equal the spend in the audit logs,
     i.e. no open holds and the logs have caught up. Otherwise stop, having changed nothing.
  2. delete those counters, as a node lost without a replica would
  3. run the same rebuild as the hourly job
  4. pass only if every counter is back to exactly its value before the wipe

During the few seconds between 2 and 3 the drill team's budget is unenforced, so the drill is
refused outside dev and local, and only touches the team it is given.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from typing import Callable

from .budget import BudgetStore, budget_key
from .logging import AuditLog
from .rebuild import logged_spend, month_start, rebuild

ALLOWED_ENVIRONMENTS = {"local", "dev"}


class DrillRefused(Exception):
    pass


def drill(budget: BudgetStore, read: Callable[[str], int], wipe: Callable[[list[str]], None],
          rows: list[tuple[str, str, int]], team: str, now: float, audit: AuditLog) -> dict:
    month, _ = month_start(now)
    expected: dict[str, int] = defaultdict(int)
    for row_team, sub, micro in rows:
        if row_team == team:
            expected[budget_key("team", team, month)] += micro
            expected[budget_key("user", sub, month)] += micro
    if not expected:
        raise DrillRefused(f"team {team!r} has no logged spend this month; run make test-live-aws first")

    before = {k: read(k) for k in expected}
    behind = {k: (before[k], v) for k, v in expected.items() if before[k] != v}
    if behind:
        raise DrillRefused("ledger and logs differ (open holds, or logs not ingested yet); nothing was "
                           f"changed, retry in a few minutes: {behind}")

    wipe(list(expected))                                   # simulated loss
    wiped = {k: read(k) for k in expected}
    summary = rebuild(budget, rows, now, audit)            # what the hourly job does
    after = {k: read(k) for k in expected}

    ok = all(v == 0 for v in wiped.values()) and after == before
    result = {"team": team, "month": month, "passed": ok, "rebuild": {k: v for k, v in summary.items() if k != "details"},
              "counters": [{"key": k, "before_usd": before[k] / 1e6, "wiped_usd": wiped[k] / 1e6,
                            "after_usd": after[k] / 1e6} for k in sorted(expected)]}
    audit.event("ledger_drill", team=team, outcome="passed" if ok else "failed",
                detail=json.dumps({"counters": len(expected)}))
    return result


def main(argv: list[str] | None = None) -> int:
    from .budget import RedisBudget
    from .rebuild import connect

    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--team", required=True)
    team = args.parse_args(argv).team
    env = os.environ
    environment = env.get("ENVIRONMENT", "").strip().lower()
    if environment not in ALLOWED_ENVIRONMENTS:
        print(json.dumps({"passed": False, "refused": f"ENVIRONMENT={environment or '(unset)'}; "
                                                       f"drills run only in {sorted(ALLOWED_ENVIRONMENTS)}"}))
        return 2

    client, logs = connect(env)
    now = time.time()
    try:
        result = drill(RedisBudget(client), lambda k: int(client.get(k) or 0), lambda ks: client.delete(*ks),
                       logged_spend(logs, env["GATEWAY_LOG_GROUP"], now), team, now, AuditLog())
    except DrillRefused as e:
        print(json.dumps({"passed": False, "refused": str(e)}))
        return 2
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
