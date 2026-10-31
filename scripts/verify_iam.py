#!/usr/bin/env python3
"""Post-deploy IAM evidence: simulate the gateway task role against a matrix of Bedrock (and SSM)
calls with `iam simulate-principal-policy`, and fail if any decision differs from the design.

    AWS_PROFILE=<your-profile> .venv/bin/python scripts/verify_iam.py      # writes docs/evidence/iam-simulation.md

Simulation evaluates the policies we wrote, using context keys we supply. It cannot tell us which
keys Bedrock actually populates at runtime; scripts/verify_guardrail.sh covers that with real calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
TF = ["terraform", f"-chdir={ROOT / 'terraform/envs/dev'}", "output", "-json"]


def outputs() -> dict:
    return {k: v["value"] for k, v in json.loads(subprocess.check_output(TF)).items()}


def main() -> int:
    o = outputs()
    region = os.environ.get("AWS_REGION", "us-east-1")
    account = boto3.client("sts").get_caller_identity()["Account"]
    role, vpce = o["task_role_arn"], o["bedrock_vpce_id"]
    g_arn, g_ver = o["guardrail_arn"], o["guardrail_version"]
    profile = f"arn:aws:bedrock:{region}:{account}:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0"
    other_profile = f"arn:aws:bedrock:{region}:{account}:inference-profile/us.anthropic.claude-opus-5-5"
    model = "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
    policy_param = f"arn:aws:ssm:{region}:{account}:parameter{o['policy_parameter_name']}"

    def ctx(**kv):
        return [{"ContextKeyName": k, "ContextKeyValues": [v], "ContextKeyType": "string"} for k, v in kv.items()]

    good = {"aws:SourceVpce": vpce, "bedrock:GuardrailIdentifier": f"{g_arn}:{g_ver}"}
    cases = [
        # (name, action, resource, context, expected decision)
        ("Converse via profile, VPCE, our guardrail (ARN:version)", "bedrock:InvokeModel", profile, good, "allowed"),
        ("Converse via profile, VPCE, our guardrail (ARN)", "bedrock:InvokeModel", profile,
         {**good, "bedrock:GuardrailIdentifier": g_arn}, "allowed"),
        ("ConverseStream via profile, our guardrail", "bedrock:InvokeModelWithResponseStream", profile, good, "allowed"),
        ("no guardrail", "bedrock:InvokeModel", profile, {"aws:SourceVpce": vpce}, "explicitDeny"),
        ("no guardrail (stream)", "bedrock:InvokeModelWithResponseStream", profile, {"aws:SourceVpce": vpce},
         "explicitDeny"),
        ("another guardrail", "bedrock:InvokeModel", profile,
         {**good, "bedrock:GuardrailIdentifier": g_arn.rsplit("/", 1)[0] + "/notours:1"}, "explicitDeny"),
        ("our guardrail as bare id", "bedrock:InvokeModel", profile,
         {**good, "bedrock:GuardrailIdentifier": g_arn.rsplit("/", 1)[1]}, "explicitDeny"),
        ("outside our VPC endpoint", "bedrock:InvokeModel", profile,
         {"bedrock:GuardrailIdentifier": f"{g_arn}:{g_ver}"}, "implicitDeny"),
        ("non-allowlisted profile", "bedrock:InvokeModel", other_profile, good, "implicitDeny"),
        ("foundation model directly (no profile)", "bedrock:InvokeModel", model, good, "implicitDeny"),
        ("foundation model via our profile", "bedrock:InvokeModel", model,
         {**good, "bedrock:InferenceProfileArn": profile}, "allowed"),
        ("async invoke", "bedrock:StartAsyncInvoke", profile, good, "implicitDeny"),
        ("batch inference job", "bedrock:CreateModelInvocationJob", "*", good, "implicitDeny"),
        ("agents", "bedrock:InvokeAgent", "*", good, "implicitDeny"),
        ("knowledge bases", "bedrock:RetrieveAndGenerate", "*", good, "implicitDeny"),
        ("change the guardrail", "bedrock:UpdateGuardrail", g_arn, good, "implicitDeny"),
        ("read gateway policy", "ssm:GetParameter", policy_param, {}, "allowed"),
        ("write gateway policy", "ssm:PutParameter", policy_param, {}, "implicitDeny"),
        ("read any other parameter", "ssm:GetParameter",
         f"arn:aws:ssm:{region}:{account}:parameter/other", {}, "implicitDeny"),
    ]

    iam = boto3.client("iam")
    rows, ok = [], True
    for name, action, resource, context, expect in cases:
        r = iam.simulate_principal_policy(PolicySourceArn=role, ActionNames=[action], ResourceArns=[resource],
                                          ContextEntries=ctx(**context))
        got = r["EvaluationResults"][0]["EvalDecision"]
        passed = got == expect
        ok &= passed
        rows.append((name, action, expect, got, passed))
        print(f"{'PASS' if passed else 'FAIL'}  {name:58} expect={expect:13} got={got}")

    out = ROOT / "docs/evidence/iam-simulation.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# IAM simulation - gateway task role\n",
             f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `scripts/verify_iam.py` against `{role}`.\n",
             "Simulation checks the policy logic with the context keys shown; runtime behaviour is "
             "checked separately by `scripts/verify_guardrail.sh`.\n",
             "| Case | Action | Expected | Got | |", "|---|---|---|---|---|"]
    lines += [f"| {n} | `{a}` | {e} | {g} | {'✅' if p else '❌'} |" for n, a, e, g, p in rows]
    out.write_text("\n".join(lines) + "\n")
    print(f"\n{'ALL PASS' if ok else 'FAILURES'} -> {out.relative_to(ROOT)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
