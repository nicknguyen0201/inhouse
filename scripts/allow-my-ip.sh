#!/usr/bin/env bash
# Point the instance's SSH rule at wherever you are now.
#
# Security group rules do not expire, so connecting from a new network and
# adding a rule leaves the old address allowed forever -- and on a campus or
# public network that address belongs to someone else within hours. This
# revokes every existing SSH rule before adding the current one, so the group
# holds exactly one and there is nothing to remember.
#
#   ./scripts/allow-my-ip.sh
#
# Only needed for manual SSH. The nightly workflow runs from a GitHub runner
# with its own address and does not use this rule.

set -euo pipefail

GROUP_ID="${SSH_SECURITY_GROUP:-sg-0126beecf8941dc9c}"
REGION="${AWS_REGION:-us-east-2}"

MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')
[[ "$MY_IP" =~ ^[0-9.]+$ ]] || { echo "could not determine public IP" >&2; exit 1; }
echo "current address: $MY_IP"

# Existing SSH rules, whatever they are.
EXISTING=$(aws ec2 describe-security-groups --group-ids "$GROUP_ID" --region "$REGION" \
  --query 'SecurityGroups[0].IpPermissions[?ToPort==`22`].IpRanges[].CidrIp' --output text)

for cidr in $EXISTING; do
  if [ "$cidr" = "$MY_IP/32" ]; then
    echo "already allowed — nothing to do"
    exit 0
  fi
  echo "revoking stale rule: $cidr"
  aws ec2 revoke-security-group-ingress --group-id "$GROUP_ID" --region "$REGION" \
    --protocol tcp --port 22 --cidr "$cidr" >/dev/null
done

aws ec2 authorize-security-group-ingress --group-id "$GROUP_ID" --region "$REGION" \
  --protocol tcp --port 22 --cidr "$MY_IP/32" >/dev/null
echo "allowed $MY_IP/32 on $GROUP_ID"
