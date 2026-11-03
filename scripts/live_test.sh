#!/usr/bin/env bash
# Run the black-box abuse tests (pytest -m live) against the deployed API.
#   - GATEWAY_URL       = terraform output api_base_url, minus /v1 (the tests add /v1/...)
#   - GATEWAY_TOKEN     = a Cognito access token for $LIVE_USER, a member of team $LIVE_TEAM
#   - GATEWAY_TOKEN_A/B = users in the dev-only live-test team (A has a $0 budget)
#   - GATEWAY_TOKEN_C   = a research user
#   - GATEWAY_LOG_GROUP = the gateway's audit log, for checks like "Bedrock was never called"
# Test users are created if missing. Each run sets a fresh random password per user, uses it once to
# get a 15-minute token, and never stores or prints it.
# Two pytest runs: everything that never reaches Bedrock, then the Bedrock-calling tests serially and
# paced under BEDROCK_RPM (default 10, the account's Haiku 4.5 requests-per-minute quota).
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
export AWS_PAGER=""
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing $PY: run make venv" >&2; exit 1; }
TF="terraform -chdir=$ROOT/terraform/envs/dev output -raw"
POOL=$($TF user_pool_id); CLIENT=$($TF cli_client_id); BASE=$($TF api_base_url)
LIVE_USER=${LIVE_USER:-slg-live-test}; LIVE_TEAM=${LIVE_TEAM:-platform}
export GATEWAY_LOG_GROUP=${GATEWAY_LOG_GROUP:-/slg-dev/gateway} AWS_REGION=${AWS_REGION:-us-east-1}

# token_for USER TEAM -> prints a fresh access token for USER, a member of TEAM only.
token_for() {
  local user=$1 team=$2 pw token
  if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$user" >/dev/null 2>&1; then
    aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$user" --message-action SUPPRESS >/dev/null
    echo "created test user $user ($team)" >&2
  fi
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$user" --group-name "$team" >/dev/null
  pw=$("$PY" -c '
import secrets, string
pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!#%*+-=?@^_"]
pw = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(28)]
secrets.SystemRandom().shuffle(pw)
print("".join(pw))')
  aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$user" --password "$pw" --permanent >/dev/null
  token=$(aws cognito-idp initiate-auth --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
    --auth-parameters "USERNAME=$user,PASSWORD=$pw" --query 'AuthenticationResult.AccessToken' --output text)
  [ -n "$token" ] && [ "$token" != None ] || { echo "could not get a token for $user" >&2; return 1; }
  printf '%s' "$token"
}

GATEWAY_TOKEN=$(token_for "$LIVE_USER" "$LIVE_TEAM")
GATEWAY_TOKEN_A=$(token_for slg-live-a live-test)
GATEWAY_TOKEN_B=$(token_for slg-live-b live-test)
GATEWAY_TOKEN_C=$(token_for slg-live-c research)
export GATEWAY_URL="${BASE%/v1}" GATEWAY_TOKEN GATEWAY_TOKEN_A GATEWAY_TOKEN_B GATEWAY_TOKEN_C
echo "running live tests against $GATEWAY_URL as $LIVE_USER ($LIVE_TEAM), slg-live-a/b (live-test), slg-live-c (research)"
cd "$ROOT"
rc=0
"$PY" -m pytest -q -m "live and not bedrock" -rfEs "$@" || rc=$?
echo "bedrock-calling tests: serial, at most ${BEDROCK_RPM:-10} calls/minute"
"$PY" -m pytest -q -m "live and bedrock" -p no:xdist -rfEs "$@" || rc=$?
exit $rc
