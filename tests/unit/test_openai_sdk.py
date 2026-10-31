"""The official OpenAI Python SDK works against the gateway by changing only base_url and api_key."""

import pytest

openai = pytest.importorskip("openai")

from conftest import mint  # noqa: E402


def client(stack, token=None, **kw):
    return openai.OpenAI(base_url="http://testserver/v1", api_key=token or mint(),
                         http_client=stack.client, **kw)


def test_chat_completion(stack):
    resp = client(stack).chat.completions.create(
        model="fast", messages=[{"role": "user", "content": "Reply with: pong"}], max_tokens=20)
    assert resp.choices[0].message.content == "pong"
    assert resp.usage.prompt_tokens == 12 and resp.usage.completion_tokens == 3


def test_raw_headers_visible(stack):
    raw = client(stack).chat.completions.with_raw_response.create(
        model="fast", messages=[{"role": "user", "content": "hi"}], max_tokens=20)
    assert raw.headers["x-request-id"] and float(raw.headers["x-budget-remaining-usd"]) > 0


def test_models_list(stack):
    ids = [m.id for m in client(stack, mint(team="research")).models.list()]
    assert ids == ["fast", "open"]


def test_sampling_passthrough(stack):
    client(stack).chat.completions.create(model="open", messages=[{"role": "user", "content": "hi"}],
                                          temperature=0.4, max_tokens=10)
    assert stack.fake.calls[0]["inferenceConfig"]["temperature"] == 0.4


def test_stream_rejected_with_named_error(stack):
    with pytest.raises(openai.BadRequestError) as e:
        client(stack).chat.completions.create(model="fast", messages=[{"role": "user", "content": "hi"}],
                                              stream=True)
    assert e.value.code == "unsupported_parameter" and e.value.param == "stream"


def test_unknown_param_rejected(stack):
    with pytest.raises(openai.BadRequestError) as e:
        client(stack).chat.completions.create(model="fast", messages=[{"role": "user", "content": "hi"}],
                                              extra_body={"api_base": "http://169.254.169.254"})
    assert e.value.code == "unknown_parameter" and e.value.param == "api_base"


def test_bad_token_is_authentication_error(stack):
    with pytest.raises(openai.AuthenticationError):
        client(stack, "not-a-token").chat.completions.create(model="fast", messages=[{"role": "user", "content": "x"}])


def test_model_not_allowed_is_permission_error(stack):
    with pytest.raises(openai.PermissionDeniedError) as e:
        client(stack, mint(team="research")).chat.completions.create(
            model="smart", messages=[{"role": "user", "content": "x"}])
    assert e.value.code == "model_not_allowed"


def test_sdk_does_not_retry_budget_exceeded(stack):
    """x-should-retry: false stops the SDK's automatic retries, so one denied call = one request."""
    c = client(stack, mint(user="frank", team="research"), max_retries=3)
    c.chat.completions.create(model="fast", messages=[{"role": "user", "content": "x"}], max_tokens=5)
    key = next(k for k in stack.budget.data if k.startswith("slg:budget:user:sub-frank:"))
    stack.budget.data[key] = 5_000_000
    before = len(stack.logs())
    with pytest.raises(openai.RateLimitError) as e:
        c.chat.completions.create(model="fast", messages=[{"role": "user", "content": "x"}], max_tokens=5)
    assert e.value.code == "budget_exceeded"
    assert len(stack.logs()) - before == 1
