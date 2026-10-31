"""Guardrail-enforcement probe. Runs as a one-off ECS task with the gateway's task role, inside the
VPC, and calls Bedrock Converse directly, bypassing the gateway on purpose, to prove that IAM (not
just application code) refuses calls without our guardrail.

    python -m app.probe        (scripts/verify_guardrail.sh runs it and checks the output)

Prints one JSON line per case: {"case", "expect", "got", "pass"}. "allowed" counts as pass for the
positive case if Bedrock returns a response OR throttles: either way IAM let the call through.
"""

from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "us-east-1")
GUARDRAIL = os.environ["BEDROCK_GUARDRAIL_ID"]          # ARN
VERSION = os.environ["BEDROCK_GUARDRAIL_VERSION"]
ALLOWED_PROFILE = os.environ.get("PROBE_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
OTHER_PROFILE = os.environ.get("PROBE_OTHER_MODEL", "us.anthropic.claude-opus-5-5")
DIRECT_MODEL = ALLOWED_PROFILE.split(".", 1)[1]           # foundation model id, no inference profile
OTHER_GUARDRAIL = GUARDRAIL.rsplit("/", 1)[0] + "/notours0000"


def call(model: str, guardrail: dict | None) -> str:
    client = boto3.client("bedrock-runtime", region_name=REGION)
    req = {"modelId": model, "messages": [{"role": "user", "content": [{"text": "say ok"}]}],
           "inferenceConfig": {"maxTokens": 5}}
    if guardrail:
        req["guardrailConfig"] = {**guardrail, "trace": "disabled"}
    try:
        client.converse(**req)
        return "allowed"
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("ThrottlingException", "ServiceQuotaExceededException"):
            return "allowed"  # authorization passed; capacity didn't
        return code


def main() -> int:
    ours = {"guardrailIdentifier": GUARDRAIL, "guardrailVersion": VERSION}
    cases = [
        ("allowlisted profile + our guardrail", ALLOWED_PROFILE, ours, "allowed"),
        ("allowlisted profile, no guardrail", ALLOWED_PROFILE, None, "AccessDeniedException"),
        ("allowlisted profile, another guardrail", ALLOWED_PROFILE,
         {"guardrailIdentifier": OTHER_GUARDRAIL, "guardrailVersion": "1"}, "AccessDeniedException"),
        ("direct foundation model + our guardrail", DIRECT_MODEL, ours, "AccessDeniedException"),
        ("non-allowlisted profile + our guardrail", OTHER_PROFILE, ours, "AccessDeniedException"),
    ]
    ok = True
    for name, model, guardrail, expect in cases:
        got = call(model, guardrail)
        passed = got == expect
        ok &= passed
        print(json.dumps({"probe": "guardrail", "case": name, "expect": expect, "got": got, "pass": passed}),
              flush=True)
    print(json.dumps({"probe": "guardrail", "summary": "PASS" if ok else "FAIL"}), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
