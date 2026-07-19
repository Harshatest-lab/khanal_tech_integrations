#!/usr/bin/env bash
# Create or reset the single Walmart dashboard Cognito user (run once after deploy).
#
# Usage:
#   export STAGE=dev
#   export DASHBOARD_EMAIL='you@example.com'
#   export DASHBOARD_PASSWORD='YourSecurePass123'
#   bash scripts/create_walmart_dashboard_user.sh
#
set -euo pipefail

STAGE="${STAGE:-dev}"
REGION="${AWS_REGION:-ap-south-1}"
SERVICE="b2c-invoice"
POOL_NAME="${SERVICE}-${STAGE}-dashboard"
EMAIL="${DASHBOARD_EMAIL:?Set DASHBOARD_EMAIL}"
PASSWORD="${DASHBOARD_PASSWORD:?Set DASHBOARD_PASSWORD}"

POOL_ID="$(aws cognito-idp list-user-pools --max-results 60 --region "$REGION" \
  --query "UserPools[?Name=='${POOL_NAME}'].Id | [0]" --output text)"

if [[ -z "$POOL_ID" || "$POOL_ID" == "None" ]]; then
  echo "User pool '${POOL_NAME}' not found in ${REGION}. Deploy b2c-invoice first." >&2
  exit 1
fi

echo "User pool: $POOL_ID"

if aws cognito-idp admin-get-user --user-pool-id "$POOL_ID" --username "$EMAIL" --region "$REGION" >/dev/null 2>&1; then
  echo "User exists — resetting password"
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$POOL_ID" \
    --username "$EMAIL" \
    --password "$PASSWORD" \
    --permanent \
    --region "$REGION"
else
  echo "Creating user $EMAIL"
  aws cognito-idp admin-create-user \
    --user-pool-id "$POOL_ID" \
    --username "$EMAIL" \
    --user-attributes "Name=email,Value=${EMAIL}" "Name=email_verified,Value=true" \
    --message-action SUPPRESS \
    --region "$REGION"
  aws cognito-idp admin-set-user-password \
    --user-pool-id "$POOL_ID" \
    --username "$EMAIL" \
    --password "$PASSWORD" \
    --permanent \
    --region "$REGION"
fi

echo "Done. Sign in at the dashboard with $EMAIL"
