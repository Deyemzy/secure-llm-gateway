"""Black-box abuse tests against a running gateway (local uvicorn, the ALB, or API Gateway).

    GATEWAY_URL=https://... GATEWAY_TOKEN=<access token for a 'platform' user> pytest -m live
"""

import base64
import json
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.live


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


def test_happy_path(live):
    status, body, headers = call(live, "/v1/chat/completions", chat())
    assert status == 200, body
    assert json.loads(body)["choices"][0]["message"]["content"]
    h = {k.lower(): v for k, v in headers.items()}
    assert h.get("x-request-id") and float(h["x-budget-remaining-usd"]) >= 0


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
