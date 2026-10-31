# Guardrail enforcement probe (runtime)

Generated 2026-10-31 20:16 UTC by `scripts/verify_guardrail.sh`: one-off ECS task `e8f1eecb489343feb139c70549786beb`,
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
