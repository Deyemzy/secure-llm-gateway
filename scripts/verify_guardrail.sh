#!/usr/bin/env bash
# Post-deploy runtime evidence: run `python -m app.probe` as a one-off ECS task with the gateway's
# task role, inside the VPC, and check that Bedrock (via IAM) refuses Converse without our guardrail.
# Writes docs/evidence/guardrail-probe.md.
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
: "${AWS_REGION:=us-east-1}"; export AWS_REGION
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TF="terraform -chdir=$ROOT/terraform/envs/dev output -raw"
CLUSTER=$($TF cluster_name); FAMILY=$($TF task_definition_family); SG=$($TF task_security_group_id)
SUBNETS=$(terraform -chdir="$ROOT/terraform/envs/dev" output -json private_subnet_ids | tr -d '[]" \n')

TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"gateway","command":["python","-m","app.probe"]}]}' \
  --query 'tasks[0].taskArn' --output text)
echo "probe task: $TASK"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
EXIT=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
LOGS=$(aws logs get-log-events --log-group-name "/$CLUSTER/gateway" --log-stream-name "gateway/gateway/${TASK##*/}" \
  --query 'events[].message' --output json | python3 -c 'import json,sys; [print(m) for m in json.load(sys.stdin)]')
echo "$LOGS"

OUT="$ROOT/docs/evidence/guardrail-probe.md"; mkdir -p "$(dirname "$OUT")"
{
  echo "# Guardrail enforcement probe (runtime)"
  echo
  echo "Generated $(date -u '+%Y-%m-%d %H:%M UTC') by \`scripts/verify_guardrail.sh\`: one-off ECS task \`${TASK##*/}\`,"
  echo "gateway task role, inside the VPC, calling Bedrock Converse **directly** (bypassing the gateway)."
  echo "\"allowed\" includes throttling: IAM authorized the call even if capacity was exhausted."
  echo
  echo "| Case | Expected | Got | |"; echo "|---|---|---|---|"
  echo "$LOGS" | python3 -c '
import json, sys
for line in sys.stdin:
    try: r = json.loads(line)
    except ValueError: continue
    if r.get("probe") == "guardrail" and "case" in r:
        print(f"| {r[\"case\"]} | {r[\"expect\"]} | {r[\"got\"]} | {\"✅\" if r[\"pass\"] else \"❌\"} |")'
  echo
  echo "Exit code: $EXIT"
} > "$OUT"
echo "-> ${OUT#$ROOT/}"
[ "$EXIT" = "0" ]
