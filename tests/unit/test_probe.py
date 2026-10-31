"""The guardrail probe's own logic (the AWS run is scripts/verify_guardrail.sh)."""

import importlib
import json

from botocore.exceptions import ClientError

ARN = "arn:aws:bedrock:us-east-1:123456789012:guardrail/abc123"


class FakeBedrock:
    """Behaves like the IAM policy we deploy: allowlisted profile + our guardrail only."""

    def converse(self, **req):
        g = req.get("guardrailConfig", {}).get("guardrailIdentifier")
        if req["modelId"] != "us.anthropic.claude-haiku-4-5-20251001-v1:0" or g != ARN:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "Converse")
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")  # authorized, no capacity


def run(monkeypatch, capsys, fake):
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", ARN)
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "1")
    import app.probe as probe
    probe = importlib.reload(probe)
    monkeypatch.setattr(probe.boto3, "client", lambda *a, **k: fake)
    code = probe.main()
    return code, [json.loads(l) for l in capsys.readouterr().out.splitlines()]


def test_probe_passes_when_iam_enforces(monkeypatch, capsys):
    code, rows = run(monkeypatch, capsys, FakeBedrock())
    assert code == 0 and rows[-1]["summary"] == "PASS" and len(rows) == 6


def test_probe_fails_when_guardrail_not_enforced(monkeypatch, capsys):
    class Lax(FakeBedrock):
        def converse(self, **req):
            if "guardrailConfig" not in req:
                return {}
            return super().converse(**req)
    code, rows = run(monkeypatch, capsys, Lax())
    assert code == 1 and rows[-1]["summary"] == "FAIL"
    assert any(r.get("case") == "allowlisted profile, no guardrail" and not r["pass"] for r in rows)
