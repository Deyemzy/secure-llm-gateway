# Guardrail enforcement probe (runtime)

Generated 2026-11-03 11:44 UTC by `scripts/verify_guardrail.sh`: one-off ECS task `111c151d297140c094ea7ce540730c70`,
gateway task role, inside the VPC, calling Bedrock Converse **directly** (bypassing the gateway).
"allowed" includes throttling: IAM authorized the call even if capacity was exhausted.
The "another guardrail" case uses the stack's decoy guardrail: real, but never ours to use.

| Case | Expected | Got | |
|---|---|---|---|
| allowlisted profile + our guardrail | allowed | allowed | ✅ |
| allowlisted profile, no guardrail | AccessDeniedException | AccessDeniedException | ✅ |
| allowlisted profile, decoy guardrail | AccessDeniedException | AccessDeniedException | ✅ |
| direct foundation model + our guardrail | AccessDeniedException | AccessDeniedException | ✅ |
| non-allowlisted profile + our guardrail | AccessDeniedException | AccessDeniedException | ✅ |

Exit code: 0
