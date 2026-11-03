#!/usr/bin/env bash
# Post-deploy runtime evidence: run `python -m app.probe` as a one-off ECS task with the gateway's
# task role, inside the VPC, and check that Bedrock (via IAM) refuses Converse without OUR guardrail:
# no guardrail, and the stack's decoy guardrail (real, but never ours to use).
# Writes docs/evidence/guardrail-probe.md.
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
: "${AWS_REGION:=us-east-1}"; export AWS_REGION
export AWS_PAGER=""
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing $PY: run make venv" >&2; exit 1; }
TF="terraform -chdir=$ROOT/terraform/envs/dev output -raw"
CLUSTER=$($TF cluster_name); FAMILY=$($TF task_definition_family); SG=$($TF task_security_group_id)
SUBNETS=$(terraform -chdir="$ROOT/terraform/envs/dev" output -json private_subnet_ids | tr -d '[]" \n')

# The decoy guardrail (terraform: module.bedrock_access.aws_bedrock_guardrail.decoy): real, so the
# "another guardrail" case tests IAM rather than request validation.
DECOY_ARN=$($TF decoy_guardrail_arn); DECOY_VERSION=$($TF decoy_guardrail_version)
echo "decoy guardrail: ${DECOY_ARN##*/} (version $DECOY_VERSION)"

OVERRIDES=$("$PY" - "$DECOY_ARN" "$DECOY_VERSION" <<'PYEOF'
import json, sys
print(json.dumps({"containerOverrides": [{"name": "gateway", "command": ["python", "-m", "app.probe"],
    "environment": [{"name": "PROBE_OTHER_GUARDRAIL_ARN", "value": sys.argv[1]},
                    {"name": "PROBE_OTHER_GUARDRAIL_VERSION", "value": sys.argv[2]}]}]}))
PYEOF
)
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
echo "probe task: $TASK"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
EXIT=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
LOGFILE=$(mktemp)
aws logs get-log-events --log-group-name "/$CLUSTER/gateway" --log-stream-name "gateway/gateway/${TASK##*/}" \
  --query 'events[].message' --output json > "$LOGFILE"

OUT="$ROOT/docs/evidence/guardrail-probe.md"; mkdir -p "$(dirname "$OUT")"
"$PY" - "$LOGFILE" "$OUT" "${TASK##*/}" "$EXIT" <<'PYEOF'
import json, sys
from datetime import datetime, timezone
logfile, out, task, exit_code = sys.argv[1:5]
rows = []
for line in json.load(open(logfile)):
    print(line)
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if r.get("probe") == "guardrail" and "case" in r:
        rows.append(r)
md = [
    "# Guardrail enforcement probe (runtime)",
    "",
    f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `scripts/verify_guardrail.sh`: a one-off ECS task,",
    "gateway task role, inside the VPC, calling Bedrock Converse **directly** (bypassing the gateway).",
    '"allowed" includes throttling: IAM authorized the call even if capacity was exhausted.',
    'The "another guardrail" case uses the stack\'s decoy guardrail: real, but never ours to use.',
    "",
    "| Case | Expected | Got | |",
    "|---|---|---|---|",
]
md += [f"| {r['case']} | {r['expect']} | {r['got']} | {'✅' if r['pass'] else '❌'} |" for r in rows]
md += ["", f"Exit code: {exit_code}"]
open(out, "w").write("\n".join(md) + "\n")
PYEOF
rm -f "$LOGFILE"
echo "-> ${OUT#$ROOT/}"
[ "$EXIT" = "0" ]
