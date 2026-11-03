"""Ledger-rebuild drill (app.drill): wipes one team's counters and requires the rebuild to restore them."""

import time

import pytest

from app import drill as drill_mod
from app.drill import DrillRefused, drill
from conftest import auth, chat_body
from test_rebuild import Audit, insights


def traffic(stack):
    for user, team in (("alice", "platform"), ("bob", "platform"), ("carol", "research")):
        r = stack.client.post("/v1/chat/completions", json=chat_body(), headers=auth(user=user, team=team))
        assert r.status_code == 200


def run(stack, team="platform", budget=None):
    data = stack.budget.data
    return drill(budget or stack.budget, lambda k: data.get(k, 0), lambda ks: [data.pop(k, None) for k in ks],
                 insights(stack.logs()), team, time.time(), Audit())


def test_drill_wipes_and_restores_exactly(stack):
    traffic(stack)
    other = {k: v for k, v in stack.budget.data.items() if ":research:" in k or "sub-carol" in k}
    result = run(stack)
    assert result["passed"]
    assert len(result["counters"]) == 3                       # platform team + alice + bob
    for c in result["counters"]:
        assert c["before_usd"] > 0 and c["wiped_usd"] == 0 and c["after_usd"] == c["before_usd"]
    assert {k: stack.budget.data[k] for k in other} == other   # other teams never touched


def test_drill_fails_when_rebuild_does_not_restore(stack):
    traffic(stack)

    class Broken:
        def raise_floor(self, key, floor):
            return 0

    assert run(stack, budget=Broken())["passed"] is False


def test_drill_refuses_with_open_hold_and_changes_nothing(stack):
    traffic(stack)
    key = next(k for k in stack.budget.data if k.startswith("slg:budget:team:platform:"))
    stack.budget.data[key] += 1234                            # an in-flight reservation the logs can't see
    snapshot = dict(stack.budget.data)
    with pytest.raises(DrillRefused, match="nothing was changed"):
        run(stack)
    assert stack.budget.data == snapshot


def test_drill_refuses_team_without_spend(stack):
    traffic(stack)
    with pytest.raises(DrillRefused, match="no logged spend"):
        run(stack, team="live-test")


@pytest.mark.parametrize("environment", ["prod", "", "staging"])
def test_drill_refused_outside_dev(monkeypatch, capsys, environment):
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setattr("app.rebuild.connect", lambda env: pytest.fail("must not connect"))
    assert drill_mod.main(["--team", "live-test"]) == 2
    assert '"refused"' in capsys.readouterr().out
