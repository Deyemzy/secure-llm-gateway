"""Black-box abuse tests against a running gateway (local uvicorn, the ALB, or API Gateway).

    GATEWAY_URL=https://... GATEWAY_TOKEN=<access token for a 'platform' user> pytest -m live

scripts/live_test.sh also sets the isolation-test users (dev policy, terraform/envs/dev/live-test-policy.yaml):
    GATEWAY_TOKEN_A   live-test team, $0 user budget
    GATEWAY_TOKEN_B   live-test team, normal budget
    GATEWAY_TOKEN_C   research team
and GATEWAY_LOG_GROUP, so tests can check the gateway's own audit record of a request.

Tests marked `bedrock` call the model. live_test.sh runs them as a separate, serial pytest run, and
each call waits so the run stays under BEDROCK_RPM (default 10, the account's Haiku 4.5 quota).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live

BEDROCK_INTERVAL_S = 60 / float(os.environ.get("BEDROCK_RPM", "10"))
_last_bedrock_call = 0.0


def call(live, path, body=None, token="__default__", method=None, raw=None):
    url, default = live
    token = default if token == "__default__" else token
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def bedrock_call(live, path, body):
    """call(), spaced at least BEDROCK_INTERVAL_S after the previous Bedrock-reaching call."""
    global _last_bedrock_call
    wait = _last_bedrock_call + BEDROCK_INTERVAL_S - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    try:
        return call(live, path, body)
    finally:
        _last_bedrock_call = time.monotonic()


def headers_of(headers):
    return {k.lower(): v for k, v in headers.items()}


def audit_record(headers, timeout_s=120):
    """The gateway's audit-log record for this response's X-Request-Id (CloudWatch Logs)."""
    group = os.environ.get("GATEWAY_LOG_GROUP")
    if not group:
        pytest.skip("GATEWAY_LOG_GROUP not set: response checks passed, audit-log checks not run")
    import boto3
    logs = boto3.client("logs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    rid = headers_of(headers)["x-request-id"]
    since = int((time.time() - 900) * 1000)
    deadline = time.monotonic() + timeout_s
    while True:
        events = logs.filter_log_events(logGroupName=group, startTime=since,
                                        filterPattern=f'{{ $.event = "request" && $.request_id = "{rid}" }}')["events"]
        if events:
            return json.loads(events[0]["message"])
        if time.monotonic() > deadline:
            pytest.fail(f"no audit record for request {rid} after {timeout_s}s")
        time.sleep(5)


def chat(text="Reply with the single word: pong", model="fast", **extra):
    return {"model": model, "messages": [{"role": "user", "content": text}], "max_tokens": 20, **extra}


def code_of(body):
    try:
        return json.loads(body)["error"]["code"]
    except Exception:
        return None


def forged(live, alg="none"):
    """Re-sign the caller's own token claims with no signature, escalating the team."""
    _, token = live
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    claims["cognito:groups"] = ["platform"]
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': alg, 'typ': 'JWT', 'kid': 'x'})}.{enc(claims)}."


@pytest.mark.bedrock
def test_happy_path(live):
    status, body, headers = bedrock_call(live, "/v1/chat/completions", chat())
    assert status == 200, body
    assert json.loads(body)["choices"][0]["message"]["content"]
    h = headers_of(headers)
    assert h.get("x-request-id") and float(h["x-budget-remaining-usd"]) >= 0


@pytest.mark.bedrock
def test_model_access_denied_is_clean_and_not_charged(live):
    """smart (Sonnet 5.5) is allowlisted for platform, but this AWS account can't use it."""
    results = [bedrock_call(live, "/v1/chat/completions", chat(model="smart")) for _ in range(2)]
    for status, body, headers in results:
        assert status == 502 and code_of(body) == "upstream_access_denied", body
        assert headers_of(headers)["x-should-retry"] == "false"
        assert "AccessDenied" not in body and "sonnet" not in body.lower()
    first, second = (headers_of(h)["x-budget-remaining-usd"] for _, _, h in results)
    assert first == second  # the second refusal left the balance exactly where the first did
    rec = audit_record(results[1][2])
    assert rec["upstream_called"] is True and rec["ledger_outcome"] == "released"
    assert rec["detail"] == "AccessDeniedException" and "cost_usd" not in rec


def test_zero_budget_user_429_and_bedrock_never_called(live_as):
    a = live_as("A")
    status, body, headers = call(a, "/v1/chat/completions", chat())
    assert status == 429 and code_of(body) == "budget_exceeded", body
    assert "monthly user budget" in json.loads(body)["error"]["message"]
    h = headers_of(headers)
    assert h["x-should-retry"] == "false" and h["x-budget-remaining-usd"] == "0.000000"
    rec = audit_record(headers)
    assert rec["team"] == "live-test" and rec["upstream_called"] is False and "ledger_outcome" not in rec


@pytest.mark.bedrock
def test_same_team_user_served_and_charged(live_as):
    """User A (same team) being at $0 must not affect user B; B's balance drops with real spend."""
    b = live_as("B")
    first = bedrock_call(b, "/v1/chat/completions", chat())
    second = bedrock_call(b, "/v1/chat/completions", chat())
    assert first[0] == 200 and second[0] == 200, (first[1], second[1])
    r1, r2 = (float(headers_of(h)["x-budget-remaining-usd"]) for _, _, h in (first, second))
    assert r2 < r1 < 0.50  # live-test user budget: $0.50
    rec = audit_record(second[2])
    assert rec["team"] == "live-test" and rec["ledger_outcome"] == "settled" and rec["cost_usd"] > 0


def test_research_user_403_on_smart(live_as):
    c = live_as("C")
    status, body, headers = call(c, "/v1/chat/completions", chat(model="smart"))
    assert status == 403 and code_of(body) == "model_not_allowed", body
    rec = audit_record(headers)
    assert rec["team"] == "research" and rec["upstream_called"] is False


def test_models(live):
    status, body, _ = call(live, "/v1/models")
    assert status == 200 and json.loads(body)["data"]


@pytest.mark.parametrize("token", [None, "", "not.a.jwt", "eyJhbGciOiJIUzI1NiJ9.e30.x"])
def test_401(live, token):
    status, _, _ = call(live, "/v1/chat/completions", chat(), token=token)
    assert status == 401


def test_unsigned_forged_token_401(live):
    status, _, _ = call(live, "/v1/chat/completions", chat(), token=forged(live))
    assert status == 401


@pytest.mark.parametrize("extra,code", [
    ({"api_base": "http://169.254.169.254/latest"}, "unknown_parameter"),
    ({"provider": "bedrock"}, "unknown_parameter"),
    ({"modelId": "anthropic.claude-opus-5-5"}, "unknown_parameter"),
    ({"stream": True}, "unsupported_parameter"),
    ({"tools": [{"type": "function"}]}, "unsupported_parameter"),
    ({"n": 4}, "unsupported_parameter"),
])
def test_rejected_fields_400(live, extra, code):
    status, body, _ = call(live, "/v1/chat/completions", chat(**extra))
    assert status == 400 and code_of(body) == code, body


def test_unknown_model_403(live):
    status, body, _ = call(live, "/v1/chat/completions", chat(model="opus"))
    assert status == 403 and code_of(body) == "model_not_allowed"


def test_raw_model_id_403(live):
    status, body, _ = call(live, "/v1/chat/completions", chat(model="us.anthropic.claude-sonnet-5-5"))
    assert status == 403


def test_credential_in_prompt_400(live):
    status, body, _ = call(live, "/v1/chat/completions", chat("here is my key AKIAIOSFODNN7EXAMPLE"))
    assert status == 400 and code_of(body) == "sensitive_data"


def test_oversized_400(live):
    status, _, _ = call(live, "/v1/chat/completions", raw=b'{"model":"fast","messages":[{"role":"user","content":"'
                        + b"x" * 300_000 + b'"}]}')
    assert status in (400, 403, 413)  # gateway 400, or WAF/API Gateway at the edge


@pytest.mark.parametrize("path,method", [("/docs", "GET"), ("/openapi.json", "GET"), ("/admin", "GET"),
                                         ("/v1/embeddings", "POST"), ("/key/generate", "POST")])
def test_other_routes_unreachable(live, path, method):
    status, _, _ = call(live, path, {} if method == "POST" else None, method=method)
    assert status in (401, 403, 404, 405), f"{method} {path} -> {status}"


def test_errors_are_openai_shaped(live):
    status, body, _ = call(live, "/v1/chat/completions", chat(stream=True))
    err = json.loads(body)["error"]
    assert set(err) >= {"message", "type", "code", "param"} and err["param"] == "stream"
