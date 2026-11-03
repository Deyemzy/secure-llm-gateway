"""The dev policy (gateway/policy.yaml + terraform/envs/dev/live-test-policy.yaml) and the isolation
scenarios the live tests run against it, here with a fake Bedrock so they also hold without AWS."""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from app.policy import Policy
from conftest import PROD_POLICY_PATH, ROOT, auth, chat_body, make_stack

OVERLAY_PATH = ROOT / "terraform" / "envs" / "dev" / "live-test-policy.yaml"
URL = "/v1/chat/completions"


def dev_policy() -> dict:
    """Same merge as locals.policy in terraform/envs/dev/main.tf."""
    base, extra = yaml.safe_load(PROD_POLICY_PATH.read_text()), yaml.safe_load(OVERLAY_PATH.read_text())
    return {**base,
            "teams": {**base["teams"], **extra["teams"]},
            "budgets": {**base["budgets"],
                        "teams": {**base["budgets"]["teams"], **extra["budgets"]["teams"]},
                        "users": {**base["budgets"]["users"], **extra["budgets"]["users"]}}}


@pytest.fixture
def dev(verifier):
    path = Path(tempfile.mkdtemp(prefix="slg-dev-")) / "policy.json"
    path.write_text(json.dumps(dev_policy()))  # published to SSM as JSON
    return make_stack(verifier, path)


def test_overlay_only_adds_never_overrides():
    base, extra = yaml.safe_load(PROD_POLICY_PATH.read_text()), yaml.safe_load(OVERLAY_PATH.read_text())
    assert set(extra) <= {"teams", "budgets"} and set(extra["budgets"]) <= {"teams", "users"}
    assert not set(extra["teams"]) & set(base["teams"])
    assert not set(extra["budgets"]["teams"]) & set(base["budgets"]["teams"])
    assert not set(extra["budgets"]["users"]) & set(base["budgets"].get("users", {}))


def test_live_test_team_is_dev_only():
    assert "live-test" not in yaml.safe_load(PROD_POLICY_PATH.read_text())["teams"]
    p = Policy.from_yaml(json.dumps(dev_policy()))
    assert p.allowed_models("live-test") == ["fast"]
    assert p.limits("live-test", "slg-live-a").user_micro_usd == 0
    assert p.limits("live-test", "slg-live-b").user_micro_usd > 0


def test_zero_budget_user_429_and_bedrock_never_called(dev):
    r = dev.client.post(URL, json=chat_body(), headers=auth(user="slg-live-a", team="live-test"))
    assert r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded"
    assert "monthly user budget" in r.json()["error"]["message"]
    assert r.headers["x-should-retry"] == "false" and r.headers["x-budget-remaining-usd"] == "0.000000"
    assert dev.fake.calls == []
    log = dev.logs()[-1]
    assert log["upstream_called"] is False and log["code"] == "budget_exceeded" and "ledger_outcome" not in log


def test_same_team_user_unaffected_and_charged(dev):
    dev.client.post(URL, json=chat_body(), headers=auth(user="slg-live-a", team="live-test"))
    first = dev.client.post(URL, json=chat_body(), headers=auth(user="slg-live-b", team="live-test"))
    second = dev.client.post(URL, json=chat_body(), headers=auth(user="slg-live-b", team="live-test"))
    assert first.status_code == second.status_code == 200
    r1, r2 = (float(r.headers["x-budget-remaining-usd"]) for r in (first, second))
    assert r1 < 0.50 and r2 < r1
    assert [lg["ledger_outcome"] for lg in dev.logs()[-2:]] == ["settled", "settled"]


def test_research_user_403_on_smart(dev):
    r = dev.client.post(URL, json=chat_body(model="smart"), headers=auth(user="slg-live-c", team="research"))
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"
    assert dev.fake.calls == [] and dev.logs()[-1]["upstream_called"] is False


def test_model_access_denied_is_clean_and_not_charged(dev):
    dev.fake.error = ("AccessDeniedException", {})
    r = dev.client.post(URL, json=chat_body(model="smart"), headers=auth(user="slg-live-test", team="platform"))
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_access_denied"
    assert r.headers["x-should-retry"] == "false"
    assert "AccessDenied" not in r.text and "sonnet" not in r.text.lower()
    assert r.headers["x-budget-remaining-usd"] == "25.000000"   # platform user budget, untouched
    assert dev.budget.resv == {}
    log = dev.logs()[-1]
    assert log["upstream_called"] is True and log["ledger_outcome"] == "released"
    assert log["detail"] == "AccessDeniedException" and "cost_usd" not in log
