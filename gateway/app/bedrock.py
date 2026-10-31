"""Bedrock Converse with the Guardrail attached, so prompt and output are screened in one call.
One API shape covers Claude, Llama and Nova.

Every failure is classified by what it means for billing, because the budget ledger depends on it:

  confirmed unbilled  -> the request was refused before any generation: release the hold
  unknown outcome     -> Bedrock may have generated (and billed) but we lost the answer:
                         charge the hold, which is a proven upper bound; never assume zero

No automatic retries: a hidden retry after a lost-but-billed response would bill twice against
one hold. Clients retry explicitly, and each retry is reserved and budgeted on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

import boto3
from botocore.config import Config
from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError,
                                 ReadTimeoutError)

# Refused before generation: nothing billed.
THROTTLE_CODES = {"ThrottlingException", "ServiceQuotaExceededException", "TooManyRequestsException"}
UNAVAILABLE_CODES = {"ServiceUnavailableException", "ModelNotReadyException"}
# The model may have run: outcome (and bill) unknown.
UNKNOWN_CODES = {"InternalServerException", "ModelTimeoutException", "ModelErrorException",
                 "ModelStreamErrorException"}


class UpstreamThrottled(Exception):
    def __init__(self, retry_after: int = 5):
        super().__init__("throttled")
        self.retry_after = retry_after


class UpstreamUnavailable(Exception):
    pass


class UpstreamOutcomeUnknown(Exception):
    """Bedrock may have generated and billed, but the gateway has no usage figures."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class UpstreamRejected(Exception):
    """Bedrock refused the call for a reason the caller can't fix (access, config, validation)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ConverseResult:
    text: str
    stop_reason: str
    input_tokens: int
    output_tokens: int

    @property
    def guardrail_blocked(self) -> bool:
        return self.stop_reason in ("guardrail_intervened", "content_filtered")


def make_client(region: str):
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=Config(connect_timeout=3, read_timeout=90, retries={"mode": "standard", "total_max_attempts": 1}),  # no retries at all
    )


class Bedrock:
    def __init__(self, client, guardrail_id: str | None, guardrail_version: str | None):
        self.client = client
        self.guardrail = (
            {"guardrailIdentifier": guardrail_id, "guardrailVersion": guardrail_version, "trace": "disabled"}
            if guardrail_id else None
        )

    def converse(self, model_id: str, system: list[str], turns: list[tuple[str, str]], max_tokens: int,
                 stop: list[str] | None = None, sampling: dict[str, float] | None = None,
                 request_metadata: dict[str, str] | None = None) -> ConverseResult:
        inference = {"maxTokens": max_tokens}
        if stop:
            inference["stopSequences"] = stop
        if sampling and "temperature" in sampling:
            inference["temperature"] = sampling["temperature"]
        if sampling and "top_p" in sampling:
            inference["topP"] = sampling["top_p"]
        req = {
            "modelId": model_id,
            "messages": [{"role": role, "content": [{"text": text}]} for role, text in turns],
            "inferenceConfig": inference,
        }
        if system:
            req["system"] = [{"text": s} for s in system]
        if self.guardrail:
            req["guardrailConfig"] = self.guardrail
        if request_metadata:
            # Appears in Bedrock model-invocation logs: lets an expired_charged hold be reconciled
            # against what Bedrock actually billed, by request id.
            req["requestMetadata"] = request_metadata

        try:
            resp = self.client.converse(**req)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code in THROTTLE_CODES:
                hdr = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after", "")
                raise UpstreamThrottled(int(hdr) if hdr.isdigit() else 5) from None
            if code in UNAVAILABLE_CODES:
                raise UpstreamUnavailable(code) from None
            if code in UNKNOWN_CODES:
                raise UpstreamOutcomeUnknown(code) from None
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
            if status >= 500:  # unrecognised server-side failure: can't rule out generation
                raise UpstreamOutcomeUnknown(code or str(status)) from None
            raise UpstreamRejected(code or "unknown") from None  # 4xx: refused, unbilled
        except (EndpointConnectionError, ConnectTimeoutError) as e:
            raise UpstreamUnavailable(type(e).__name__) from None   # never reached Bedrock
        except (ReadTimeoutError, ConnectionClosedError) as e:
            raise UpstreamOutcomeUnknown(type(e).__name__) from None  # sent; answer lost

        content = resp.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in content)
        usage = resp.get("usage", {})
        return ConverseResult(text, resp.get("stopReason", ""), int(usage.get("inputTokens", 0)),
                              int(usage.get("outputTokens", 0)))
