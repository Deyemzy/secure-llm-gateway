# IAM simulation - gateway task role

Generated 2026-10-31 19:57 UTC by `scripts/verify_iam.py` against `arn:aws:iam::830088750088:role/slg-dev-task`.

Simulation checks the policy logic with the context keys shown; runtime behaviour is checked separately by `scripts/verify_guardrail.sh`.

| Case | Action | Expected | Got | |
|---|---|---|---|---|
| Converse via profile, VPCE, our guardrail (ARN:version) | `bedrock:InvokeModel` | allowed | allowed | ✅ |
| Converse via profile, VPCE, our guardrail (ARN) | `bedrock:InvokeModel` | allowed | allowed | ✅ |
| ConverseStream via profile, our guardrail | `bedrock:InvokeModelWithResponseStream` | allowed | allowed | ✅ |
| no guardrail | `bedrock:InvokeModel` | explicitDeny | explicitDeny | ✅ |
| no guardrail (stream) | `bedrock:InvokeModelWithResponseStream` | explicitDeny | explicitDeny | ✅ |
| another guardrail | `bedrock:InvokeModel` | explicitDeny | explicitDeny | ✅ |
| our guardrail as bare id | `bedrock:InvokeModel` | explicitDeny | explicitDeny | ✅ |
| outside our VPC endpoint | `bedrock:InvokeModel` | implicitDeny | implicitDeny | ✅ |
| non-allowlisted profile | `bedrock:InvokeModel` | implicitDeny | implicitDeny | ✅ |
| foundation model directly (no profile) | `bedrock:InvokeModel` | implicitDeny | implicitDeny | ✅ |
| foundation model via our profile | `bedrock:InvokeModel` | allowed | allowed | ✅ |
| async invoke | `bedrock:StartAsyncInvoke` | implicitDeny | implicitDeny | ✅ |
| batch inference job | `bedrock:CreateModelInvocationJob` | implicitDeny | implicitDeny | ✅ |
| agents | `bedrock:InvokeAgent` | implicitDeny | implicitDeny | ✅ |
| knowledge bases | `bedrock:RetrieveAndGenerate` | implicitDeny | implicitDeny | ✅ |
| change the guardrail | `bedrock:UpdateGuardrail` | implicitDeny | implicitDeny | ✅ |
| read gateway policy | `ssm:GetParameter` | allowed | allowed | ✅ |
| write gateway policy | `ssm:PutParameter` | implicitDeny | implicitDeny | ✅ |
| read any other parameter | `ssm:GetParameter` | implicitDeny | implicitDeny | ✅ |
