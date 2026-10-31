#!/usr/bin/env bash
# Manage gateway users in Cognito and fetch access tokens.
#   scripts/cognito.sh add-user alice platform      # prompts for a password
#   scripts/cognito.sh token alice                  # prints a 15-minute access token
#   scripts/cognito.sh disable-user alice
set -euo pipefail
# AWS credentials: the standard chain (export AWS_PROFILE=... to pick a named profile).
TF="terraform -chdir=$(dirname "$0")/../terraform/envs/dev"
POOL=$($TF output -raw user_pool_id)
CLIENT=$($TF output -raw cli_client_id)

read_password() {
  read -rsp "Password for $1: " PW; echo >&2
}

case "${1:-}" in
  add-user)
    user=$2 team=$3
    read_password "$user"
    aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$user" --message-action SUPPRESS >/dev/null
    aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$user" --password "$PW" --permanent
    aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$user" --group-name "$team"
    echo "created $user in team $team" >&2 ;;
  token)
    user=$2
    read_password "$user"
    aws cognito-idp initiate-auth --client-id "$CLIENT" --auth-flow USER_PASSWORD_AUTH \
      --auth-parameters "USERNAME=$user,PASSWORD=$PW" --query 'AuthenticationResult.AccessToken' --output text ;;
  disable-user)
    aws cognito-idp admin-disable-user --user-pool-id "$POOL" --username "$2"
    aws cognito-idp admin-user-global-sign-out --user-pool-id "$POOL" --username "$2"
    echo "disabled $2 (existing access tokens remain valid until they expire, <=15 min)" >&2 ;;
  *) sed -n '2,5p' "$0"; exit 1 ;;
esac
