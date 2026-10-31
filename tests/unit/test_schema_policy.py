"""Strict schema, named rejections, policy resolution, budget precedence, fail-closed config."""

import pytest
import yaml

from app.policy import ConfigError, Policy, PolicyDenied, PolicyStore, input_token_upper_bound
from app.schema import RequestRejected, parse
from conftest import POLICY_PATH


def body(**kw):
    base = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return base


def rejected(payload, code, param=None):
    with pytest.raises(RequestRejected) as e:
        parse(payload)
    assert e.value.code == code, e.value.message
    if param:
        assert e.value.param == param
    return e.value


def test_minimal_request():
    r = parse(body())
    assert r.max_tokens == 1024 and r.sampling == {} and r.stop_sequences == []


def test_accepted_fields():
    r = parse(body(max_tokens=50, temperature=0.2, stop="END", stream=False, n=1))
    assert r.sampling == {"temperature": 0.2} and r.stop_sequences == ["END"]
    assert parse(body(top_p=0.9, stop=["a", "b"])).sampling == {"top_p": 0.9}


def test_text_parts_accepted():
    r = parse(body(messages=[{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]))
    assert r.messages[0].text == "ab"


# ---- named rejections ------------------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("stream", True), ("tools", []), ("tools", [{"type": "function"}]), ("tool_choice", "auto"),
    ("functions", []), ("function_call", "auto"), ("n", 2), ("n", 10),
])
def test_unsupported_parameter(field, value):
    e = rejected(body(**{field: value}), "unsupported_parameter", field)
    assert e.message.startswith(f"unsupported_parameter: {field}")


def test_image_content_unsupported():
    rejected(body(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]),
             "unsupported_parameter", "messages.0.content")


@pytest.mark.parametrize("field", ["api_base", "base_url", "provider", "custom_llm_provider", "aws_access_key_id",
                                   "modelId", "guardrailConfig", "metadata", "extra_body", "user", "logit_bias",
                                   "response_format", "seed"])
def test_unknown_parameter(field):
    e = rejected(body(**{field: "x"}), "unknown_parameter", field)
    assert e.message == f"unknown_parameter: {field}"


def test_unknown_field_inside_message():
    rejected(body(messages=[{"role": "user", "content": "hi", "name": "x"}]), "unknown_parameter", "messages.0.name")


@pytest.mark.parametrize("bad", [
    {"model": ""}, {"model": "Fast"}, {"model": "../etc"}, {"model": "x" * 65}, {"model": 1},
    {"messages": []}, {"messages": "hi"}, {"max_tokens": 0}, {"max_tokens": 100_000}, {"max_tokens": "100"},
    {"max_tokens": 10.5}, {"max_tokens": True}, {"stop": ["a"] * 5}, {"stop": [""]},
    {"temperature": 1.5}, {"temperature": -0.1}, {"top_p": 0}, {"temperature": "0.5"},
])
def test_type_and_range_violations(bad):
    rejected(body(**bad), "invalid_request")


@pytest.mark.parametrize("roles", [
    ["assistant"], ["user", "user"], ["user", "assistant"], ["system"], ["user", "system", "user"],
])
def test_conversation_shape(roles):
    rejected(body(messages=[{"role": r, "content": "x"} for r in roles]), "invalid_request")


@pytest.mark.parametrize("roles", [["tool"], ["developer", "user"]])
def test_other_roles_rejected(roles):
    rejected(body(messages=[{"role": r, "content": "x"} for r in roles]), "invalid_request")


def test_rejections_do_not_echo_values():
    e = rejected(body(max_tokens="SUPERSECRET"), "invalid_request")
    assert "SUPERSECRET" not in e.message


# ---- resolution ----------------------------------------------------------------------

def test_resolve_alias(policy):
    assert policy.resolve("platform", "smart", 1024, {}).model_id == "us.anthropic.claude-sonnet-5-5"
    assert policy.resolve("research", "open", 1024, {"temperature": 0.3}).model_id.startswith("us.meta.llama4")


@pytest.mark.parametrize("team,alias,code", [
    ("research", "smart", "model_not_allowed"),
    ("platform", "gpt-4o", "model_not_allowed"),
    ("platform", "us.anthropic.claude-sonnet-5-5", "model_not_allowed"),
    ("intruders", "fast", "team_not_allowed"),
])
def test_resolve_denials(policy, team, alias, code):
    with pytest.raises(PolicyDenied) as e:
        policy.resolve(team, alias, 1024, {})
    assert e.value.code == code and e.value.status == 403


def test_per_model_max_tokens(policy):
    with pytest.raises(PolicyDenied) as e:
        policy.resolve("platform", "fast", 8000, {})
    assert e.value.code == "max_tokens_not_allowed"


def test_sampling_rules(policy):
    with pytest.raises(PolicyDenied) as e:
        policy.resolve("platform", "smart", 100, {"temperature": 0.5})
    assert e.value.code == "unsupported_parameter" and e.value.param == "temperature" and e.value.status == 400
    with pytest.raises(PolicyDenied) as e:
        policy.resolve("platform", "fast", 100, {"temperature": 0.5, "top_p": 0.9})
    assert e.value.param == "top_p"
    assert policy.resolve("platform", "open", 100, {"temperature": 0.5, "top_p": 0.9})


def test_budget_precedence_user_team_default(policy):
    alice = policy.limits("platform", "alice")      # users.alice override
    bob = policy.limits("platform", "bob")          # teams.platform.user_monthly_usd
    carol = policy.limits("research", "carol")      # default.user_monthly_usd
    assert alice.user_micro_usd == 60_000_000 and bob.user_micro_usd == 25_000_000
    assert carol.user_micro_usd == 5_000_000
    assert alice.team_micro_usd == 200_000_000 and carol.team_micro_usd == 40_000_000


def test_team_limit_falls_back_to_default():
    cfg = yaml.safe_load(POLICY_PATH.read_text())
    del cfg["budgets"]["teams"]["research"]
    assert Policy.from_yaml(yaml.safe_dump(cfg)).limits("research", "x").team_micro_usd == 50_000_000


def test_cost_math(policy):
    route = policy.resolve("platform", "smart", 1024, {})
    assert route.cost_micro_usd(1000, 500) == 7_000  # $2/$10 per MTok


def test_input_upper_bound_is_conservative():
    assert input_token_upper_bound(["héllo"]) == len("héllo".encode()) + 16


# ---- fail-closed config -----------------------------------------------------------------

def mutate(fn):
    cfg = yaml.safe_load(POLICY_PATH.read_text())
    fn(cfg)
    return yaml.safe_dump(cfg)


@pytest.mark.parametrize("change,needle", [
    (lambda c: c["models"]["open"].pop("output_usd_per_mtok"), "open"),
    (lambda c: c["models"]["fast"].update(input_usd_per_mtok=0), "fast"),
    (lambda c: c["models"]["smart"].update(input_usd_per_mtok=None), "smart"),
    (lambda c: c["teams"]["research"]["models"].append("ghost"), "ghost"),
])
def test_unpriced_or_unknown_allowed_model_refused(change, needle):
    with pytest.raises(ConfigError) as e:
        Policy.from_yaml(mutate(change))
    assert needle in str(e.value)


def test_unpriced_model_not_allowed_anywhere_is_fine():
    def add(c):
        c["models"]["spare"] = {"model_id": "x.y.z", "max_tokens": 10}
    Policy.from_yaml(mutate(add))


@pytest.mark.parametrize("text", [
    "", "not: [valid", "[]", "models: {}",
    mutate(lambda c: c.pop("budgets")),
    mutate(lambda c: c["budgets"].pop("default")),
    mutate(lambda c: c["budgets"]["default"].update(user_monthly_usd=-1)),
    mutate(lambda c: c["budgets"]["teams"].update(ghosts={"team_monthly_usd": 1})),
    mutate(lambda c: c.update(surprise=True)),
    mutate(lambda c: c["screening"]["secrets"].update(bad="(")),
])
def test_invalid_policy_refused(text):
    with pytest.raises(ConfigError):
        Policy.from_yaml(text)


def test_store_first_load_failure_is_fatal():
    with pytest.raises(ConfigError):
        PolicyStore(lambda: (_ for _ in ()).throw(OSError("ssm unreachable")))


def test_store_keeps_last_good_on_bad_refresh():
    good = POLICY_PATH.read_text()
    state = {"text": good}
    t = [0.0]
    store = PolicyStore(lambda: state["text"], refresh_s=60, clock=lambda: t[0])
    first = store.current()
    state["text"] = "garbage: ["
    t[0] += 61
    assert store.current() is first                    # invalid refresh ignored
    state["text"] = mutate(lambda c: c["budgets"]["users"].update(bob={"monthly_usd": 1}))
    t[0] += 61
    assert store.current().limits("platform", "bob").user_micro_usd == 1_000_000


# ---- screening -----------------------------------------------------------------------------

@pytest.mark.parametrize("text,name", [
    ("my key AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIE", "private_key"),
    ("tok ghp_" + "a" * 36, "github_token"),
    ("AKIA​IOSFODNN7EXAMPLE", "aws_access_key"),
])
def test_credentials_blocked(policy, text, name):
    with pytest.raises(PolicyDenied) as e:
        policy.screen_input([text])
    assert e.value.code == "sensitive_data" and e.value.status == 400 and name in e.value.message


def test_pii_redacted(policy):
    s = policy.screen_input(["Email jane@example.com, SSN 123-45-6789, card 4111 1111 1111 1111, call 415-555-0100"])
    for leaked in ("jane@example.com", "123-45-6789", "4111 1111 1111 1111", "415-555-0100"):
        assert leaked not in s.texts[0]
    assert s.redactions == {"email": 1, "us_ssn": 1, "credit_card": 1, "phone_us": 1}


def test_output_screening(policy):
    text, counts = policy.screen_output("key AKIAIOSFODNN7EXAMPLE mail bob@corp.io")
    assert "AKIA" not in text and "bob@corp.io" not in text
    assert counts == {"aws_access_key": 1, "email": 1}


def test_production_policy_loads_and_keeps_open_out_until_priced():
    from conftest import PROD_POLICY_PATH
    prod = Policy.from_yaml(PROD_POLICY_PATH.read_text())
    assert all("open" not in t.models for t in prod.cfg.teams.values())
    assert prod.routes["smart"].max_tokens <= 1024  # finishes inside the 29 s REST timeout



def test_rejected_refresh_raises_alarm_immediately():
    """'A broken update never removes controls, and the team knows immediately.'"""
    alarms = []
    t = [0.0]
    state = {"text": POLICY_PATH.read_text()}
    store = PolicyStore(lambda: state["text"], refresh_s=60, max_stale_s=300, clock=lambda: t[0],
                        on_reject=alarms.append)
    good = store.current()
    state["text"] = "teams: {oops"
    t[0] = 61
    assert store.current() is good                         # controls still in force
    assert len(alarms) == 1 and "not valid YAML" in alarms[0]
