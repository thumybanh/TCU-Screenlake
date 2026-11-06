#!/usr/bin/env bash
# One-shot migration: flag existing test users with role=TEST on both
# DynamoDB (pre-reservation + user records) and Cognito (custom:tcu_role).
#
# Run once after redeploying the Lambda with role support. Idempotent — safe
# to rerun if any single user update fails midway.

set -euo pipefail

REGION="us-east-2"
POOL_ID="us-east-2_5pvOLMS20"
TABLE="screenlake-tcu-codes"

# email -> (sub, code)
declare -a USERS=(
  "jacksonberger518@gmail.com|0003"
  "jgcordova07@gmail.com|0002"
  "kclockton@gmail.com|0004"
  "jessie.wangkexin@gmail.com|1002"
  "mythubanh@gmail.com|"   # code auto-detected below
)

lookup_sub() {
    local email="$1"
    aws cognito-idp list-users \
        --user-pool-id "$POOL_ID" \
        --region "$REGION" \
        --filter "email = \"$email\"" \
        --query 'Users[0].Attributes[?Name==`sub`].Value|[0]' \
        --output text
}

lookup_code() {
    local sub="$1"
    aws dynamodb get-item \
        --table-name "$TABLE" \
        --region "$REGION" \
        --key "{\"id\":{\"S\":\"user#${sub}\"}}" \
        --query 'Item.tcu_code.S' \
        --output text 2>/dev/null || echo ""
}

for entry in "${USERS[@]}"; do
    email="${entry%|*}"
    code="${entry#*|}"

    echo ""
    echo "== $email =="

    sub=$(lookup_sub "$email")
    if [ -z "$sub" ] || [ "$sub" = "None" ]; then
        echo "  no Cognito user found for $email — skipping"
        continue
    fi
    echo "  sub: $sub"

    if [ -z "$code" ]; then
        code=$(lookup_code "$sub")
        if [ -z "$code" ] || [ "$code" = "None" ]; then
            echo "  no DynamoDB user#$sub record — skipping"
            continue
        fi
    fi
    echo "  code: $code"

    # DynamoDB: patch user#<sub>
    aws dynamodb update-item \
        --table-name "$TABLE" \
        --region "$REGION" \
        --key "{\"id\":{\"S\":\"user#${sub}\"}}" \
        --update-expression "SET #r = :r" \
        --expression-attribute-names '{"#r":"role"}' \
        --expression-attribute-values '{":r":{"S":"TEST"}}' >/dev/null
    echo "  DynamoDB user#$sub -> role=TEST"

    # DynamoDB: patch pre#<email> if it exists
    if aws dynamodb get-item \
        --table-name "$TABLE" \
        --region "$REGION" \
        --key "{\"id\":{\"S\":\"pre#${email}\"}}" \
        --query 'Item' --output text 2>/dev/null | grep -q .; then
        aws dynamodb update-item \
            --table-name "$TABLE" \
            --region "$REGION" \
            --key "{\"id\":{\"S\":\"pre#${email}\"}}" \
            --update-expression "SET #r = :r" \
            --expression-attribute-names '{"#r":"role"}' \
            --expression-attribute-values '{":r":{"S":"TEST"}}' >/dev/null
        echo "  DynamoDB pre#$email -> role=TEST"
    fi

    # Cognito: patch custom:tcu_role
    aws cognito-idp admin-update-user-attributes \
        --user-pool-id "$POOL_ID" \
        --region "$REGION" \
        --username "$sub" \
        --user-attributes Name=custom:tcu_role,Value=TEST >/dev/null
    echo "  Cognito $sub -> custom:tcu_role=TEST"
done

echo ""
echo "Done."
