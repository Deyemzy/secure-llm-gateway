#!/usr/bin/env bash
# Post-deploy drill: run `python -m app.drill` as a one-off ECS task from the dev-only drill task
# definition (its own Valkey user slg-drill, the only one allowed DEL, on slg:budget:* keys only; a task
# role that can query the audit logs and nothing else; inside the VPC). For the dev-only live-test team it checks the ledger matches the audit
# logs, deletes the team's budget counters, runs the hourly rebuild, and requires every counter back
# at its exact value. Run it after `make test-live-aws` (which gives the team real spend), and give the
# logs a few minutes to be ingested first; if they haven't caught up the drill changes nothing.
# The rebuild's raises fire the LedgerRaised metric, so expect that alarm: it's part of the drill.
# Writes docs/evidence/ledger-rebuild-drill.md.
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
: "${AWS_REGION:=us-east-1}"; export AWS_REGION
export AWS_PAGER=""
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing $PY: run make venv" >&2; exit 1; }
TF="terraform -chdir=$ROOT/terraform/envs/dev output -raw"
CLUSTER=$($TF cluster_name); SG=$($TF task_security_group_id)
FAMILY=$($TF drill_task_definition_family 2>/dev/null) || FAMILY=""
[ -n "$FAMILY" ] && [ "$FAMILY" != null ] || { echo "no drill task definition: set enable_ledger_drill (dev only) and apply" >&2; exit 1; }
SUBNETS=$(terraform -chdir="$ROOT/terraform/envs/dev" output -json private_subnet_ids | tr -d '[]" \n')
DRILL_TEAM=${DRILL_TEAM:-live-test}

OVERRIDES=$("$PY" -c 'import json, sys
print(json.dumps({"containerOverrides": [{"name": "drill",
    "command": ["python", "-m", "app.drill", "--team", sys.argv[1]]}]}))' "$DRILL_TEAM")
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$FAMILY" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "$OVERRIDES" --query 'tasks[0].taskArn' --output text)
echo "drill task: $TASK (team $DRILL_TEAM)"
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
EXIT=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
LOGFILE=$(mktemp)
aws logs get-log-events --log-group-name "/$CLUSTER/gateway" --log-stream-name "drill/drill/${TASK##*/}" \
  --query 'events[].message' --output json > "$LOGFILE"

OUT="$ROOT/docs/evidence/ledger-rebuild-drill.md"; mkdir -p "$(dirname "$OUT")"
"$PY" - "$LOGFILE" "$OUT" "${TASK##*/}" "$EXIT" <<'PYEOF'
import json, re, sys
from datetime import datetime, timezone
logfile, out, task, exit_code = sys.argv[1:5]
redact = lambda s: re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<cognito-sub>", s)
result = None
for line in json.load(open(logfile)):
    print(line)
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if "passed" in r:
        result = r
md = [
    "# Ledger rebuild drill (runtime)",
    "",
    f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by `scripts/drill_ledger_rebuild.sh`: a one-off ECS task,",
    "dev-only drill task (Valkey user slg-drill, logs-query-only task role), inside the VPC. The team's budget counters were checked against",
    "the audit logs, deleted (simulated data loss), and rebuilt from the logs by the hourly job's code.",
    "",
]
if result is None:
    md += ["No drill result in the task log."]
elif "refused" in result:
    md += [f"Refused, nothing changed: {result['refused']}"]
else:
    md += [f"Team `{result['team']}`, month {result['month']}: **{'passed' if result['passed'] else 'FAILED'}**", "",
           "| Counter | Before | After wipe | After rebuild | |", "|---|---|---|---|---|"]
    md += [f"| `{redact(c['key'])}` | ${c['before_usd']:.6f} | ${c['wiped_usd']:.6f} | ${c['after_usd']:.6f} | "
           f"{'✅' if c['after_usd'] == c['before_usd'] else '❌'} |" for c in result["counters"]]
    md += ["", f"Rebuild summary: `{json.dumps(result['rebuild'])}`"]
md += ["", f"Exit code: {exit_code}"]
open(out, "w").write("\n".join(md) + "\n")
PYEOF
rm -f "$LOGFILE"
echo "-> ${OUT#$ROOT/}"
[ "$EXIT" = "0" ]
