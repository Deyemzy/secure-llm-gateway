import io
import json
import os
import time
from pathlib import Path

import jwt
import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth import JWKSCache, JWTVerifier
from app.bedrock import Bedrock
from app.budget import InMemoryBudget
from app.logging import AuditLog
from app.main import Deps, create_app
from app.policy import Policy, PolicyStore, file_source

ROOT = Path(__file__).resolve().parents[1]
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT_ID = "testclient123"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk_of(key, kid):
    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**d, "kid": kid, "alg": "RS256", "use": "sig"}


JWKS = {"keys": [jwk_of(_KEY, "k1")]}


def mint(user="alice", team="platform", key=_KEY, kid="k1", alg="RS256", ttl=900, extra_claims=None, **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": f"sub-{user}", "username": user, "client_id": CLIENT_ID,
              "token_use": "access", "iat": now, "exp": now + ttl, "cognito:groups": [team]}
    claims.update(overrides)
    claims.update(extra_claims or {})  # arbitrary claim names, including ones that shadow parameters
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def verifier():
    return JWTVerifier(JWKSCache(static_jwks=JWKS), issuer=ISSUER, client_ids={CLIENT_ID})


PROD_POLICY_PATH = ROOT / "gateway" / "policy.yaml"


def _test_policy() -> Path:
    """The production policy, plus `open` allowed for both teams so multi-model behaviour stays
    covered while production keeps `open` out of allowlists until its prices are verified."""
    import tempfile
    import yaml
    cfg = yaml.safe_load(PROD_POLICY_PATH.read_text())
    cfg["teams"]["platform"]["models"] = ["fast", "smart", "open"]
    cfg["teams"]["research"]["models"] = ["fast", "open"]
    path = Path(tempfile.mkdtemp(prefix="slg-test-")) / "policy.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


POLICY_PATH = _test_policy()


@pytest.fixture
def policy():
    return Policy.from_yaml(POLICY_PATH.read_text())


class FakeConverse:
    """Stands in for the boto3 bedrock-runtime client."""

    def __init__(self):
        self.calls = []
        self.reply = "pong"
        self.stop_reason = "end_turn"
        self.usage = (12, 3)
        self.error = None
        self.delay = 0.0
        self.converse_hook = None

    def converse(self, **req):
        self.calls.append(req)
        if self.converse_hook:
            self.converse_hook()
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            code, headers = self.error
            raise ClientError({"Error": {"Code": code, "Message": "x"},
                               "ResponseMetadata": {"HTTPHeaders": headers}}, "Converse")
        return {"output": {"message": {"role": "assistant", "content": [{"text": self.reply}]}},
                "stopReason": self.stop_reason,
                "usage": {"inputTokens": self.usage[0], "outputTokens": self.usage[1]}}


def make_stack(verifier, policy_path=None):
    fake = FakeConverse()
    budget = InMemoryBudget()
    logs = io.StringIO()
    store = PolicyStore(file_source(str(policy_path or POLICY_PATH)))
    deps = Deps(verifier, store, budget, Bedrock(fake, "gr-123", "1"), AuditLog(logs))
    client = TestClient(create_app(deps), raise_server_exceptions=False)

    class Stack:
        pass

    s = Stack()
    s.client, s.fake, s.budget, s.deps, s.store = client, fake, budget, deps, store
    s.logs = lambda: [json.loads(line) for line in logs.getvalue().splitlines()]
    return s


@pytest.fixture
def stack(verifier):
    return make_stack(verifier)


def chat_body(text="Reply with: pong", model="fast", **extra):
    return {"model": model, "messages": [{"role": "user", "content": text}], "max_tokens": 64, **extra}


def auth(token=None, **kw):
    return {"Authorization": f"Bearer {token or mint(**kw)}"}


class Secret(str):
    """A str whose repr never shows its value, so pytest failure reports can't print a token."""

    def __repr__(self):
        return "'<redacted>'"


class LiveTarget(tuple):
    def __repr__(self):
        return f"LiveTarget(url={self[0]!r}, token='<redacted>')"


@pytest.fixture
def live():
    url, token = os.environ.get("GATEWAY_URL"), os.environ.get("GATEWAY_TOKEN")
    if not url or not token:
        pytest.skip("GATEWAY_URL / GATEWAY_TOKEN not set")
    return LiveTarget((url.rstrip("/"), Secret(token)))


@pytest.fixture
def live_as(live):
    """live_as("A") -> the same gateway, as the user whose token is in GATEWAY_TOKEN_A."""
    def target(who: str) -> LiveTarget:
        token = os.environ.get(f"GATEWAY_TOKEN_{who}")
        if not token:
            pytest.skip(f"GATEWAY_TOKEN_{who} not set (scripts/live_test.sh sets it)")
        return LiveTarget((live[0], Secret(token)))
    return target
