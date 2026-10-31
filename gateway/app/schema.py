"""Strict, OpenAI-compatible request subset.

Accepted:  model (alias), messages (system/user/assistant, text only), max_tokens, temperature, top_p, stop.
Rejected with a named reason:  stream=true, tools/tool_choice (and legacy functions), n>1.
Rejected as unknown:  everything else (extra="forbid"), including api_base. Blocked by
construction, not by a denylist.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

MAX_BODY_BYTES = 256 * 1024
MAX_MESSAGES = 50
MAX_INPUT_CHARS = 32_000
MAX_TOKENS_CEILING = 8192  # hard schema ceiling; per-model caps live in policy.yaml


class RequestRejected(Exception):
    """A 400 with a specific, developer-facing reason."""

    def __init__(self, code: str, param: str | None, message: str):
        super().__init__(message)
        self.code, self.param, self.message = code, param, message


# Known OpenAI parameters we deliberately don't support yet, and why.
UNSUPPORTED = {
    "stream": (lambda v: v not in (None, False), "streaming is not supported yet; send stream=false"),
    "tools": (lambda v: v is not None, "tool calling is out of scope for v1"),
    "tool_choice": (lambda v: v is not None, "tool calling is out of scope for v1"),
    "functions": (lambda v: v is not None, "function calling is out of scope for v1"),
    "function_call": (lambda v: v is not None, "function calling is out of scope for v1"),
    "parallel_tool_calls": (lambda v: v is not None, "tool calling is out of scope for v1"),
    "n": (lambda v: v not in (None, 1), "only n=1 is supported (n>1 multiplies cost)"),
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class TextPart(Strict):
    type: Literal["text"]
    text: str = Field(max_length=MAX_INPUT_CHARS)


class Message(Strict):
    role: Literal["system", "user", "assistant"]
    content: str | Annotated[list[TextPart], Field(min_length=1, max_length=64)]

    @property
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "".join(p.text for p in self.content)


class ChatRequest(Strict):
    model: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9.-]*$")
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)
    max_tokens: int = Field(default=1024, ge=1, le=MAX_TOKENS_CEILING)
    temperature: float | None = Field(default=None, ge=0, le=1)
    top_p: float | None = Field(default=None, gt=0, le=1)
    stop: str | Annotated[list[str], Field(max_length=4)] | None = None
    # Accepted only at their no-op values, so stock SDK payloads validate.
    stream: Literal[False] | None = None
    n: Literal[1] | None = None

    @model_validator(mode="after")
    def _conversation_shape(self) -> "ChatRequest":
        if any(not m.text for m in self.messages):
            raise ValueError("message content must not be empty")
        if sum(len(m.text) for m in self.messages) > MAX_INPUT_CHARS:
            raise ValueError(f"total message content exceeds {MAX_INPUT_CHARS} characters")

        # System messages only at the start; then strictly alternating user/assistant,
        # starting and ending with user (Converse requires it, and no assistant prefill).
        i = 0
        while i < len(self.messages) and self.messages[i].role == "system":
            i += 1
        rest = self.messages[i:]
        if not rest:
            raise ValueError("at least one user message is required")
        if any(m.role == "system" for m in rest):
            raise ValueError("system messages must come before the conversation")
        for n, m in enumerate(rest):
            if m.role != ("user" if n % 2 == 0 else "assistant"):
                raise ValueError("messages must alternate user/assistant, starting with user")
        if rest[-1].role != "user":
            raise ValueError("the last message must be from the user")

        if any(not s or len(s) > 64 for s in self.stop_sequences):
            raise ValueError("stop sequences must be 1-64 characters")
        return self

    @property
    def stop_sequences(self) -> list[str]:
        if self.stop is None:
            return []
        return [self.stop] if isinstance(self.stop, str) else list(self.stop)

    @property
    def sampling(self) -> dict[str, float]:
        return {k: v for k, v in (("temperature", self.temperature), ("top_p", self.top_p)) if v is not None}


def parse(payload: Any) -> ChatRequest:
    """Validate, turning failures into specific, named rejections. Never echoes submitted values."""
    if not isinstance(payload, dict):
        raise RequestRejected("invalid_request", None, "body must be a JSON object")

    for name, (is_unsupported, why) in UNSUPPORTED.items():
        if name in payload and is_unsupported(payload[name]):
            raise RequestRejected("unsupported_parameter", name, f"unsupported_parameter: {name} ({why})")

    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for i, m in enumerate(msgs):
            parts = m.get("content") if isinstance(m, dict) else None
            if isinstance(parts, list) and any(isinstance(p, dict) and p.get("type") not in (None, "text") for p in parts):
                raise RequestRejected("unsupported_parameter", f"messages.{i}.content",
                                      f"unsupported_parameter: messages.{i}.content (text only; no images or files)")

    try:
        return ChatRequest.model_validate(payload)
    except ValidationError as e:
        errors = e.errors()
        extra = next((err for err in errors if err["type"] == "extra_forbidden"), None)
        if extra:
            name = ".".join(str(x) for x in extra["loc"])
            raise RequestRejected("unknown_parameter", name, f"unknown_parameter: {name}") from None
        first = errors[0]
        loc = ".".join(str(x) for x in first["loc"]) or None
        msg = first["msg"] if first["type"] in ("value_error", "assertion_error") else first["type"]
        raise RequestRejected("invalid_request", loc, f"invalid_request: {loc or 'body'}: {msg}") from None
