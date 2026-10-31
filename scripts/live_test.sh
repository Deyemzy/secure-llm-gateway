#!/usr/bin/env bash
# Run the black-box abuse tests (pytest -m live) against the deployed API.
#   - GATEWAY_URL   = terraform output api_base_url, minus /v1 (the tests add /v1/...)
#   - GATEWAY_TOKEN = a Cognito access token for $LIVE_USER, a member of team $LIVE_TEAM
# The test user is created if missing. Each run sets a fresh random password, uses it once to get a
# 15-minute token, and never stores or prints it.
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
export AWS_PAGER=""
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "missing $PY: run make venv" >&2; exit 1; }
TF="terraform -chdir=$ROOT/terraform/envs/dev output -raw"
POOL=$($TF user_pool_id); CLIENT=$($TF cli_client_id); BASE=$($TF api_base_url)
LIVE_USER=${LIVE_USER:-slg-live-test}; LIVE_TEAM=${LIVE_TEAM:-platform}

if ! aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$LIVE_USER" >/dev/null 2>&1; then
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$LIVE_USER" --message-action SUPPRESS >/dev/null
  echo "created test user $LIVE_USER"
fi
aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$LIVE_USER" --group-name "$LIVE_TEAM"

PW=$("$PY" -c '
import secrets, string
pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!#%*+-=?@^_"]
pw = [secrets.choice(p) for p in pools] + [secrets.choice("".join(pools)) for _ in range(28)]
secrets.SystemRandom().shuffle(pw)
print("".join(pw))')
aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$LIVE_USER" --password "$PW" --permanent
TOKEN=$(aws cognito-idp initiate-auth --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters "USERNAME=$LIVE_USER,PASSWORD=$PW" --query 'AuthenticationResult.AccessToken' --output text)
unset PW

export GATEWAY_URL="${BASE%/v1}" GATEWAY_TOKEN="$TOKEN"
echo "running live tests against $GATEWAY_URL as $LIVE_USER ($LIVE_TEAM)"
cd "$ROOT" && exec "$PY" -m pytest -q -m live -rfEs "$@"
