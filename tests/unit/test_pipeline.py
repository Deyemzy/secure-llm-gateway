"""End-to-end through the FastAPI app: pipeline order, status codes, budget accounting, headers, logs."""

import pytest

from app.budget import BudgetUnavailable
from conftest import auth, chat_body, mint

URL = "/v1/chat/completions"


def post(stack, body, headers=None, **kw):
    return stack.client.post(URL, json=body, headers=headers if headers is not None else auth(**kw))


def err(r):
    return r.json()["error"]["code"]


def spent(stack):
    return sum(v for k, v in stack.budget.data.items() if k.startswith("slg:budget:team:"))


def test_happy_path(stack):
    r = post(stack, chat_body())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["object"] == "chat.completion" and j["choices"][0]["message"]["content"] == "pong"
    assert j["model"] == "fast" and j["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    call = stack.fake.calls[0]
    assert call["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert call["guardrailConfig"]["guardrailIdentifier"] == "gr-123"
    assert call["inferenceConfig"] == {"maxTokens": 64}
    # settled to actual: 12 in * $1 + 3 out * $5 = 27 micro-USD, hold released
    assert spent(stack) == 27 and stack.budget.resv == {}


def test_response_headers(stack):
    r = post(stack, chat_body())
    assert len(r.headers["x-request-id"]) == 32 and j_id(r) == f"chatcmpl-{r.headers['x-request-id']}"
    # alice: user override $60, platform team $200 -> min is the user's, minus 27 micro-USD
    assert r.headers["x-budget-remaining-usd"] == "59.999973"
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def j_id(r):
    return r.json()["id"]


def test_request_id_on_errors_and_in_logs(stack):
    r = post(stack, chat_body(), headers={})
    rid = r.headers["x-request-id"]
    assert stack.logs()[-1]["request_id"] == rid


def test_sampling_and_stop_passed_to_converse(stack):
    assert post(stack, chat_body(model="open", temperature=0.3, top_p=0.8, stop="END")).status_code == 200
    assert stack.fake.calls[0]["inferenceConfig"] == {"maxTokens": 64, "temperature": 0.3, "topP": 0.8,
                                                      "stopSequences": ["END"]}


def test_system_prompt_mapped(stack):
    body = {"model": "fast", "max_tokens": 10,
            "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]}
    assert post(stack, body).status_code == 200
    call = stack.fake.calls[0]
    assert call["system"] == [{"text": "be brief"}] and [m["role"] for m in call["messages"]] == ["user"]


# ---- step 1: identity --------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer junk"}, {"Authorization": "Basic YTpi"}])
def test_401(stack, headers):
    r = post(stack, chat_body(), headers=headers)
    assert r.status_code == 401 and err(r) == "unauthorized"
    assert r.json()["error"] == {"message": "missing or invalid token", "type": "authentication_error",
                                 "code": "unauthorized", "param": None}
    assert r.headers["x-should-retry"] == "false" and not stack.fake.calls


def test_auth_runs_before_validation(stack):
    assert post(stack, {"garbage": True}, headers={}).status_code == 401


def test_expired_token_401(stack):
    assert post(stack, chat_body(), headers={"Authorization": f"Bearer {mint(exp=1)}"}).status_code == 401


# ---- step 2: shape --------------------------------------------------------------------

@pytest.mark.parametrize("extra,code,param", [
    ({"stream": True}, "unsupported_parameter", "stream"),
    ({"tools": [{"type": "function"}]}, "unsupported_parameter", "tools"),
    ({"tool_choice": "auto"}, "unsupported_parameter", "tool_choice"),
    ({"n": 3}, "unsupported_parameter", "n"),
    ({"api_base": "http://169.254.169.254"}, "unknown_parameter", "api_base"),
    ({"provider": "openai"}, "unknown_parameter", "provider"),
    ({"modelId": "anthropic.claude-opus-5-5"}, "unknown_parameter", "modelId"),
])
def test_rejected_fields_400(stack, extra, code, param):
    r = post(stack, chat_body(**extra))
    e = r.json()["error"]
    assert r.status_code == 400 and e["code"] == code and e["param"] == param
    assert e["type"] == "invalid_request_error" and e["message"].startswith(f"{code}: {param}")
    assert not stack.fake.calls and spent(stack) == 0


def test_invalid_json_400(stack):
    r = stack.client.post(URL, content=b"{not json", headers={**auth(), "Content-Type": "application/json"})
    assert r.status_code == 400 and err(r) == "invalid_request"


def test_oversized_body_400(stack):
    r = stack.client.post(URL, content=b"x" * 300_000, headers=auth())
    assert r.status_code == 400 and err(r) == "request_too_large"


# ---- step 3: policy -----------------------------------------------------------------------

def test_model_not_allowed_403(stack):
    r = post(stack, chat_body(model="smart"), team="research")
    assert r.status_code == 403 and err(r) == "model_not_allowed" and r.json()["error"]["type"] == "permission_error"
    assert not stack.fake.calls and spent(stack) == 0


def test_unknown_team_403(stack):
    r = post(stack, chat_body(), team="intruders")
    assert r.status_code == 403 and err(r) == "team_not_allowed"


def test_sampling_not_supported_by_model_400(stack):
    r = post(stack, chat_body(model="smart", temperature=0.2))
    assert r.status_code == 400 and err(r) == "unsupported_parameter" and r.json()["error"]["param"] == "temperature"


def test_credential_in_prompt_blocked_before_bedrock(stack):
    r = post(stack, chat_body("use AKIAIOSFODNN7EXAMPLE"))
    assert r.status_code == 400 and err(r) == "sensitive_data" and not stack.fake.calls


def test_pii_redacted_before_bedrock(stack):
    assert post(stack, chat_body("mail jane@example.com")).status_code == 200
    sent = stack.fake.calls[0]["messages"][0]["content"][0]["text"]
    assert "jane@example.com" not in sent and "[REDACTED:email]" in sent


# ---- step 4: budget ------------------------------------------------------------------------

def test_budget_exceeded_429_not_retryable(stack):
    assert post(stack, chat_body(), user="carol", team="research").status_code == 200
    user_key = next(k for k in stack.budget.data if k.startswith("slg:budget:user:sub-carol:"))
    stack.budget.data[user_key] = 5_000_000  # research default user budget: $5
    n = len(stack.fake.calls)
    r = post(stack, chat_body(), user="carol", team="research")
    assert r.status_code == 429 and err(r) == "budget_exceeded" and "monthly user budget" in r.json()["error"]["message"]
    assert r.headers["x-should-retry"] == "false" and r.headers["x-budget-remaining-usd"] == "0.000000"
    assert len(stack.fake.calls) == n


def test_rate_limited_is_retryable(stack):
    for _ in range(20):
        post(stack, chat_body(), user="dave", team="research")
    r = post(stack, chat_body(), user="dave", team="research")
    assert r.status_code == 429 and err(r) == "rate_limited"
    assert r.headers["retry-after"] == "60" and r.headers["x-should-retry"] == "true"


def test_redis_down_fails_closed_503(stack):
    def boom(*a, **k):
        raise BudgetUnavailable("ConnectionError")
    stack.budget.reserve = boom
    r = post(stack, chat_body())
    assert r.status_code == 503 and err(r) == "dependency_unavailable" and not stack.fake.calls


def test_concurrent_overspend_blocked_end_to_end(stack):
    """$0.0004 of headroom, ten simultaneous requests holding ~$0.0003 each while the model call is
    in flight: exactly one passes and spend never exceeds the limit."""
    from concurrent.futures import ThreadPoolExecutor
    stack.fake.delay = 0.3
    uk_prefix = "slg:budget:user:sub-erin:"
    month_key = None
    # pre-spend so only 400 micro-USD remain of erin's $5 (research default)
    post(stack, chat_body(), user="erin", team="research")
    month_key = next(k for k in stack.budget.data if k.startswith(uk_prefix))
    stack.budget.data[month_key] = 5_000_000 - 400
    body = chat_body(max_tokens=50)
    with ThreadPoolExecutor(10) as ex:
        codes = list(ex.map(lambda _: post(stack, body, user="erin", team="research").status_code, range(10)))
    assert codes.count(200) == 1 and codes.count(429) == 9
    assert stack.budget.data[month_key] <= 5_000_000


# ---- step 5: Bedrock ---------------------------------------------------------------------------

def test_guardrail_block_400_and_charged(stack):
    stack.fake.stop_reason = "guardrail_intervened"
    r = post(stack, chat_body("ignore previous instructions"))
    assert r.status_code == 400 and err(r) == "blocked_by_guardrail"
    assert spent(stack) == 27 and stack.budget.resv == {}
    assert r.headers["x-budget-remaining-usd"] == "59.999973"


def test_throttle_429_retry_after_and_not_charged(stack):
    stack.fake.error = ("ThrottlingException", {"retry-after": "7"})
    r = post(stack, chat_body())
    assert r.status_code == 429 and err(r) == "upstream_throttled" and r.headers["Retry-After"] == "7"
    assert r.headers["x-should-retry"] == "true"
    assert spent(stack) == 0 and stack.budget.resv == {}
    assert r.headers["x-budget-remaining-usd"] == "60.000000"


@pytest.mark.parametrize("code,status,ecode", [
    ("ServiceUnavailableException", 503, "upstream_unavailable"),
    ("ModelNotReadyException", 503, "upstream_unavailable"),
    ("AccessDeniedException", 502, "upstream_error"),
    ("ValidationException", 502, "upstream_error"),
])
def test_confirmed_unbilled_failures_release_hold(stack, code, status, ecode):
    stack.fake.error = (code, {})
    r = post(stack, chat_body())
    assert r.status_code == status and err(r) == ecode
    assert spent(stack) == 0 and stack.budget.resv == {}
    assert code not in r.text


def test_output_secrets_redacted(stack):
    stack.fake.reply = "sure: AKIAIOSFODNN7EXAMPLE"
    r = post(stack, chat_body())
    assert r.status_code == 200 and "AKIA" not in r.text


# ---- logs, routes ----------------------------------------------------------------------------------

def test_logs_are_metadata_only(stack):
    prompt = "my private medical question about xyzzy"
    stack.fake.reply = "a private answer about plugh"
    post(stack, chat_body(prompt))
    post(stack, chat_body(prompt), headers={})
    logs = stack.logs()
    assert len(logs) == 2
    dump = str(logs)
    assert "xyzzy" not in dump and "plugh" not in dump
    ok = logs[0]
    assert ok["outcome"] == "allow" and ok["user"] == "alice" and ok["team"] == "platform"
    assert ok["cost_usd"] == 0.000027 and ok["input_tokens"] == 12
    assert logs[1]["code"] == "unauthorized" and logs[1]["detail"]


def test_models_lists_team_aliases_only(stack):
    r = stack.client.get("/v1/models", headers=auth(team="research"))
    assert [m["id"] for m in r.json()["data"]] == ["fast", "open"]
    assert stack.client.get("/v1/models").status_code == 401


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/admin", "/v1/embeddings", "/key/generate"])
def test_no_other_routes(stack, path):
    r = stack.client.get(path, headers=auth())
    assert r.status_code == 404 and r.json()["error"]["type"] == "not_found_error"
    assert stack.client.post(path, headers=auth(), json={}).status_code in (404, 405)


def test_self_edited_team_attribute_cannot_unlock_models_or_budget(stack):
    """A research user who set custom:team=platform on their own profile still gets research's
    allowlist and research's budget."""
    evil = {"custom:team": "platform", "team": "platform", "preferred_username": "alice"}
    r = post(stack, chat_body(model="smart"), user="mallory", team="research", extra_claims=evil)
    assert r.status_code == 403 and err(r) == "model_not_allowed"
    r = post(stack, chat_body(), user="mallory", team="research", extra_claims=evil)
    assert r.status_code == 200
    # research default user budget ($5), not platform's $25 nor alice's $60 override
    assert r.headers["x-budget-remaining-usd"] == "4.999973"


# ---- idempotency, ledger lifecycle, crash recovery, durability --------------------------------------

def test_idempotency_key_blocks_double_spend(stack):
    h = {**auth(), "Idempotency-Key": "retry-abc-123"}
    first = stack.client.post(URL, json=chat_body(), headers=h)
    assert first.status_code == 200
    again = stack.client.post(URL, json=chat_body(), headers=h)
    e = again.json()["error"]
    assert again.status_code == 409 and e["code"] == "already_completed" and again.headers["x-should-retry"] == "false"
    assert len(stack.fake.calls) == 1 and spent(stack) == 27            # one model call, charged once


def test_idempotency_key_in_flight_conflict(stack):
    from concurrent.futures import ThreadPoolExecutor
    stack.fake.delay = 0.3
    h = {**auth(), "Idempotency-Key": "inflight-key-1"}
    with ThreadPoolExecutor(2) as ex:
        codes = sorted(ex.map(lambda _: stack.client.post(URL, json=chat_body(), headers=h).status_code, range(2)))
    assert codes == [200, 409] and len(stack.fake.calls) == 1


def test_idempotency_keys_are_per_user(stack):
    for user in ("alice", "bob"):
        h = {**auth(user=user), "Idempotency-Key": "same-key-everywhere"}
        assert stack.client.post(URL, json=chat_body(), headers=h).status_code == 200


@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 129, "bad/char!x"])
def test_invalid_idempotency_key_400(stack, key):
    r = stack.client.post(URL, json=chat_body(), headers={**auth(), "Idempotency-Key": key})
    assert r.status_code == 400 and r.json()["error"]["param"] == "Idempotency-Key"


def test_request_metadata_reaches_bedrock_for_reconciliation(stack):
    r = post(stack, chat_body())
    meta = stack.fake.calls[0]["requestMetadata"]
    assert meta["gateway_request_id"] == r.headers["x-request-id"] and meta["ledger_id"]


def test_worker_crash_mid_call_is_recovered_by_reconciler(stack):
    """An unexpected exception during the model call leaves the hold open (Bedrock may have billed).
    After the deadline the reconciler marks it expired_charged: never under-counted, and logged."""
    from app.budget import HOLD_DEADLINE_S
    from app.main import run_reconcile
    now = [1_793_400_000.0]
    stack.budget.clock = lambda: now[0]
    stack.fake.converse_hook = lambda: (_ for _ in ()).throw(RuntimeError("worker died"))
    r = post(stack, chat_body())
    assert r.status_code == 500 and err(r) == "internal_error" and r.headers["x-request-id"]
    assert len(stack.budget.resv) == 1                                   # hold left open on purpose
    assert run_reconcile(stack.deps) == []                               # not due yet
    now[0] += HOLD_DEADLINE_S + 1
    [o] = run_reconcile(stack.deps)
    assert o.outcome == "expired_charged" and spent(stack) == o.amount > 0
    ev = [l for l in stack.logs() if l["event"] == "reconcile"]
    assert ev and ev[0]["outcome"] == "expired_charged" and ev[0]["ledger_id"] == o.request_id


def test_crash_before_model_call_is_refunded(stack):
    from app.budget import HOLD_DEADLINE_S
    from app.main import run_reconcile
    now = [1_793_400_000.0]
    stack.budget.clock = lambda: now[0]
    real_begin = stack.budget.begin
    stack.budget.begin = lambda r: (_ for _ in ()).throw(RuntimeError("OOM-killed"))
    assert post(stack, chat_body()).status_code == 500
    stack.budget.begin = real_begin
    now[0] += HOLD_DEADLINE_S + 1
    [o] = run_reconcile(stack.deps)
    assert o.outcome == "expired_released" and spent(stack) == 0 and not stack.fake.calls


def test_hold_not_replicated_fails_closed(stack):
    """WAIT timed out: no replica confirmed the hold. Fail closed: 503, Bedrock never called, the hold
    released (nothing was spent), and the log line the replica-ack alarm counts."""
    stack.budget.wait_replicated = lambda: False
    r = post(stack, chat_body())
    assert r.status_code == 503 and err(r) == "dependency_unavailable"
    assert r.headers["retry-after"] == "5" and r.headers["x-should-retry"] == "true"
    assert not stack.fake.calls and spent(stack) == 0 and stack.budget.resv == {}
    assert stack.logs()[-1]["detail"] == "replica_ack_timeout"


def test_wait_itself_erroring_fails_closed(stack):
    from app.budget import BudgetUnavailable
    def boom():
        raise BudgetUnavailable("TimeoutError")
    stack.budget.wait_replicated = boom
    r = post(stack, chat_body())
    assert r.status_code == 503 and not stack.fake.calls and spent(stack) == 0


def test_expired_hold_never_reaches_bedrock(stack):
    stack.budget.begin = lambda r: False
    r = post(stack, chat_body())
    assert r.status_code == 503 and err(r) == "reservation_expired" and not stack.fake.calls


# ---- policy staleness: fail closed at runtime, not just at startup ------------------------------------

def test_bad_policy_refresh_then_stale_policy_rejects_every_request(stack):
    t = [0.0]
    store = stack.store
    store.clock = lambda: t[0]
    store._valid_at = store._attempted_at = 0.0
    store.source = lambda: "models: [broken"
    t[0] = 61                                                           # refresh attempted, rejected
    assert post(stack, chat_body()).status_code == 200                  # last validated policy, bounded
    t[0] = store.max_stale_s + 1                                        # no valid refresh for too long
    for r in (post(stack, chat_body()), stack.client.get("/v1/models", headers=auth())):
        assert r.status_code == 503 and err(r) == "policy_unavailable" and r.headers["x-should-retry"] == "true"
    assert len(stack.fake.calls) == 1


# ---- team is never taken from the client -------------------------------------------------------------

@pytest.mark.parametrize("header", ["X-Team", "X-Gateway-Team", "X-Amzn-Oidc-Data", "X-Cognito-Groups",
                                    "X-Forwarded-User", "X-User"])
def test_client_supplied_team_headers_ignored(stack, header):
    h = {**auth(user="rita", team="research"), header: "platform"}
    r = stack.client.post(URL, json=chat_body(model="smart"), headers=h)
    assert r.status_code == 403 and err(r) == "model_not_allowed"


def test_client_supplied_team_body_field_rejected(stack):
    r = post(stack, chat_body(team="platform"), user="rita", team="research")
    assert r.status_code == 400 and r.json()["error"]["param"] == "team"



# ---- unknown outcomes: never guess zero ---------------------------------------------------------------

def _hold_amount(stack):
    return next(h["amount"] for h in stack.budget.holds.values())


@pytest.mark.parametrize("exc", [
    "read_timeout", "connection_closed", "InternalServerException", "ModelTimeoutException", "ModelErrorException",
    "SomeNew5xxException",
])
def test_unknown_outcome_charges_the_hold(stack, exc):
    """Bedrock may have generated and billed but the answer was lost: charge the reservation, which is a
    proven upper bound on cost. The client is told, and the SDK is told not to auto-retry."""
    from botocore.exceptions import ClientError, ConnectionClosedError, ReadTimeoutError
    errors = {
        "read_timeout": ReadTimeoutError(endpoint_url="https://bedrock-runtime"),
        "connection_closed": ConnectionClosedError(endpoint_url="https://bedrock-runtime"),
    }
    def boom():
        if exc in errors:
            raise errors[exc]
        raise ClientError({"Error": {"Code": exc}, "ResponseMetadata": {"HTTPStatusCode": 500}}, "Converse")
    stack.fake.converse_hook = boom
    r = post(stack, chat_body())
    held = _hold_amount(stack)
    assert r.status_code == 502 and err(r) == "upstream_outcome_unknown"
    assert r.headers["x-should-retry"] == "false"
    assert spent(stack) == held > 27                      # worst case charged, not zero, not actual
    assert stack.budget.resv == {}                        # closed immediately, not left for the reconciler
    state = next(h["state"] for h in stack.budget.holds.values())
    assert state == "charged_unknown"
    log = stack.logs()[-1]
    assert log["outcome_unknown"] is True and log["cost_usd"] == held / 1e6


def test_connect_failure_is_confirmed_unbilled(stack):
    from botocore.exceptions import ConnectTimeoutError
    def boom():
        raise ConnectTimeoutError(endpoint_url="https://bedrock-runtime")
    stack.fake.converse_hook = boom
    r = post(stack, chat_body())
    assert r.status_code == 503 and err(r) == "upstream_unavailable" and spent(stack) == 0


def test_bedrock_client_never_retries_silently():
    """A hidden retry after a lost-but-billed response would bill twice against one hold."""
    from app.bedrock import make_client
    c = make_client("us-east-1")
    assert c.meta.config.retries["total_max_attempts"] == 1   # initial call only; "max_attempts" counts retries


# ---- pre-invoke state is durable before Bedrock is called ---------------------------------------------

def test_preinvoke_state_is_replica_acknowledged_before_bedrock_call(stack):
    """Order is reserve -> begin(invoking) -> WAIT(replicas) -> Converse. Bedrock is never called
    unless the 'invoking' state has been acknowledged by the replicas."""
    order = []
    b = stack.budget
    real = {n: getattr(b, n) for n in ("reserve", "begin", "wait_replicated")}

    def spy(name):
        def f(*a, **k):
            order.append(name)
            return real[name](*a, **k)
        return f

    for n in real:
        setattr(b, n, spy(n))
    stack.fake.converse_hook = lambda: order.append("converse")
    assert post(stack, chat_body()).status_code == 200
    assert order == ["reserve", "begin", "wait_replicated", "converse"]


def test_crash_between_durable_preinvoke_write_and_bedrock_call(stack):
    """The worker dies AFTER the replica-acknowledged 'invoking' write and BEFORE the Converse request
    leaves the process. The ledger can't tell this from 'Bedrock billed and the answer was lost', so it
    must treat it conservatively: the hold stays charged (expired_charged), never refunded as zero."""
    from app.budget import HOLD_DEADLINE_S
    from app.main import run_reconcile
    now = [1_793_400_000.0]
    stack.budget.clock = lambda: now[0]
    acked = []
    real_wait = stack.budget.wait_replicated
    stack.budget.wait_replicated = lambda: acked.append(real_wait()) or acked[-1]

    def crash_before_send(*a, **k):
        raise SystemError("SIGKILL after durable write, before the network call")
    stack.deps.bedrock.converse = crash_before_send

    r = post(stack, chat_body())
    assert r.status_code == 500 and acked == [True] and stack.fake.calls == []   # never reached Bedrock
    [hold] = stack.budget.holds.values()
    assert hold["state"] == "invoking"                                            # durable pre-invoke state
    now[0] += HOLD_DEADLINE_S + 1
    [o] = run_reconcile(stack.deps)
    assert o.outcome == "expired_charged" and spent(stack) == hold["amount"]     # charged the ceiling


# ---- Idempotency-Key is namespaced per user ------------------------------------------------------------

def test_cross_user_idempotency_key_collision(stack):
    """Bob reusing Alice's Idempotency-Key (by accident or to probe) must neither be blocked by her
    request, nor learn it exists, nor be charged to her ledger, while hers is in flight or completed."""
    from concurrent.futures import ThreadPoolExecutor
    key = "shared-key-0001"
    alice = {**auth(user="alice"), "Idempotency-Key": key}
    bob = {**auth(user="bob"), "Idempotency-Key": key}

    stack.fake.delay = 0.3                                              # both in flight at once
    with ThreadPoolExecutor(2) as ex:
        ra, rb = ex.map(lambda h: stack.client.post(URL, json=chat_body(), headers=h), [alice, bob])
    assert (ra.status_code, rb.status_code) == (200, 200)

    stack.fake.delay = 0
    again_bob = stack.client.post(URL, json=chat_body(), headers=bob)     # bob's own key: his duplicate
    assert again_bob.status_code == 409 and err(again_bob) == "already_completed"

    ledger = {l["user"]: l["ledger_id"] for l in stack.logs() if l.get("status") == 200}
    assert ledger["alice"] != ledger["bob"]                               # different ledger records
    user_spend = {k.split(":")[3]: v for k, v in stack.budget.data.items() if k.startswith("slg:budget:user:")}
    assert user_spend == {"sub-alice": 27, "sub-bob": 27}                 # each charged once, to themselves


def test_idempotency_namespace_is_the_stable_subject_not_username(stack):
    """Scoping uses the token's `sub`: two principals that share a username (e.g. recreated account)
    still get separate idempotency namespaces."""
    key = "same-key-0002"
    first = {**auth(user="erin", extra_claims={"sub": "sub-erin-v1"}), "Idempotency-Key": key}
    second = {**auth(user="erin", extra_claims={"sub": "sub-erin-v2"}), "Idempotency-Key": key}
    assert stack.client.post(URL, json=chat_body(), headers=first).status_code == 200
    assert stack.client.post(URL, json=chat_body(), headers=second).status_code == 200


def test_lowered_budget_limit_applies_at_next_refresh(stack):
    """Revocation row 'budget limit lowered': allowed before the refresh, denied right after it."""
    import yaml
    from conftest import POLICY_PATH
    t = [0.0]
    store = stack.store
    store.clock = lambda: t[0]
    store._valid_at = store._attempted_at = 0.0
    cfg = yaml.safe_load(POLICY_PATH.read_text())
    cfg["budgets"]["users"]["alice"] = {"monthly_usd": 0}
    store.source = lambda: yaml.safe_dump(cfg)               # admin sets alice's limit to $0
    assert post(stack, chat_body()).status_code == 200       # before the refresh: old limit ($60)
    t[0] = store.refresh_s                                    # next refresh (60 s)
    r = post(stack, chat_body())
    assert r.status_code == 429 and err(r) == "budget_exceeded"
