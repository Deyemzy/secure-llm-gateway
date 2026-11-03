# Ledger rebuild drill (runtime)

Generated 2026-11-03 11:37 UTC by `scripts/drill_ledger_rebuild.sh`: a one-off ECS task,
dev-only drill task (Valkey user slg-drill, logs-query-only task role), inside the VPC. The team's budget counters were checked against
the audit logs, deleted (simulated data loss), and rebuilt from the logs by the hourly job's code.

Team `live-test`, month 2026-11: **passed**

| Counter | Before | After wipe | After rebuild | |
|---|---|---|---|---|
| `slg:budget:team:live-test:2026-11` | $0.000080 | $0.000000 | $0.000080 | ✅ |
| `slg:budget:user:<cognito-sub>:2026-11` | $0.000080 | $0.000000 | $0.000080 | ✅ |

Rebuild summary: `{"month": "2026-11", "teams": 2, "users": 2, "raised": 2}`

Exit code: 0
