"""Fail-closed startup: unsafe or incomplete configuration stops the process."""

import json

import pytest
import yaml

from app.config import DEV_ISSUER, Settings, StartupError
from conftest import JWKS, POLICY_PATH


@pytest.fixture
def dev_jwks(tmp_path):
    p = tmp_path / "jwks.json"
    p.write_text(json.dumps(JWKS))
    return str(p)


def prod_env(**over):
    env = {
        "ENVIRONMENT": "dev",
        "JWT_ISSUER": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_X",
        "JWT_CLIENT_IDS": "abc",
        "JWKS_JSON": json.dumps(JWKS),
        "REDIS_HOST": "valkey.internal",
        "POLICY_SSM_PARAMETER": "/slg-dev/policy",
        "BEDROCK_GUARDRAIL_ID": "arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def local_env(dev_jwks, **over):
    env = {"ENVIRONMENT": "local", "DEV_TOKENS_ENABLED": "true", "DEV_JWKS_PATH": dev_jwks,
           "BUDGET_BACKEND": "memory", "POLICY_PATH": str(POLICY_PATH)}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def test_valid_deployed_config():
    s = Settings.from_env(prod_env())
    assert not s.dev_tokens and not s.is_local and s.budget_backend == "redis"


def test_dev_tokens_disabled_by_default():
    assert Settings.from_env(prod_env()).dev_tokens is False


def test_dev_tokens_allowed_in_local(dev_jwks):
    s = Settings.from_env(local_env(dev_jwks))
    assert s.dev_tokens and s.jwt_issuer == DEV_ISSUER


@pytest.mark.parametrize("environment", ["dev", "prod", "staging", "LOCALHOST", "local-ish"])
def test_dev_tokens_refused_outside_local(dev_jwks, environment):
    with pytest.raises(StartupError, match="dev tokens are local-only"):
        Settings.from_env(local_env(dev_jwks, ENVIRONMENT=environment))


def test_dev_tokens_refused_when_environment_unset(dev_jwks):
    with pytest.raises(StartupError, match="ENVIRONMENT must be set"):
        Settings.from_env(local_env(dev_jwks, ENVIRONMENT=None))


def test_dev_issuer_refused_outside_local():
    with pytest.raises(StartupError, match="dev issuer"):
        Settings.from_env(prod_env(JWT_ISSUER=DEV_ISSUER))


def test_build_refuses_to_start_with_dev_tokens_outside_local(dev_jwks, monkeypatch):
    from app import main
    for k, v in local_env(dev_jwks, ENVIRONMENT="prod").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="refusing to start"):
        main.build()


@pytest.mark.parametrize("over,needle", [
    ({"BUDGET_BACKEND": "memory"}, "local-only"),
    ({"REDIS_HOST": None}, "REDIS_HOST"),
    ({"POLICY_SSM_PARAMETER": None}, "POLICY_SSM_PARAMETER"),
    ({"POLICY_SSM_PARAMETER": None, "POLICY_PATH": "/x.yaml"}, "POLICY_SSM_PARAMETER"),
    ({"BEDROCK_GUARDRAIL_ID": None}, "GUARDRAIL"),
    ({"BEDROCK_GUARDRAIL_ID": "abc123"}, "ARN"),
    ({"JWT_ISSUER": "http://insecure"}, "JWT_ISSUER"),
    ({"JWT_CLIENT_IDS": ""}, "JWT_CLIENT_IDS"),
    ({"JWKS_JSON": None}, "JWKS"),
])
def test_deployed_config_fails_closed(over, needle):
    with pytest.raises(StartupError, match=needle):
        Settings.from_env(prod_env(**over))


def test_build_refuses_unpriced_model(dev_jwks, tmp_path, monkeypatch):
    from app import main
    cfg = yaml.safe_load(POLICY_PATH.read_text())
    del cfg["models"]["open"]["input_usd_per_mtok"]
    bad = tmp_path / "policy.yaml"
    bad.write_text(yaml.safe_dump(cfg))
    for k, v in local_env(dev_jwks, POLICY_PATH=str(bad)).items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="open.*input_usd_per_mtok"):
        main.build()


def test_build_refuses_missing_policy(dev_jwks, monkeypatch):
    from app import main
    for k, v in local_env(dev_jwks, POLICY_PATH="/nonexistent/policy.yaml").items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit, match="could not load policy"):
        main.build()


def test_build_starts_with_good_local_config(dev_jwks, monkeypatch):
    from app import main
    for k, v in local_env(dev_jwks).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert main.build() is not None


@pytest.mark.parametrize("value", ["custom:team", "cognito:groups", ""])
def test_team_claim_is_not_configurable(value):
    with pytest.raises(StartupError, match="TEAM_CLAIM is not configurable"):
        Settings.from_env(prod_env(TEAM_CLAIM=value))


def test_token_lifetime_defaults_to_15_minutes():
    assert Settings.from_env(prod_env()).jwt_max_lifetime_s == 900



def test_policy_staleness_defaults_to_revocation_window():
    """Full revocation within 15 minutes: policy staleness bound == token lifetime."""
    s = Settings.from_env(prod_env())
    assert s.policy_max_stale_s == 900 == s.jwt_max_lifetime_s
