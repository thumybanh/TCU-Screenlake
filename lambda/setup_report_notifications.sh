#!/usr/bin/env bash
# Sets up S3 → SNS → email notifications for bug reports submitted via the app.
#
# Run once. Safe to re-run — topic creation and subscription are idempotent.
#
# Usage:
#   ./setup_report_notifications.sh \
#       --bucket screenlake-data \
#       --emails "mythubanh@gmail.com,researcher2@example.com"
#
# Prerequisites: AWS CLI configured with a role that has sns:*, s3:PutBucketNotificationConfiguration

set -euo pipefail

BUCKET=""
EMAILS=""
REGION="us-east-2"
TOPIC_NAME="screenlake-bug-reports"

usage() {
    echo "Usage: $0 --bucket <bucket> --emails <comma-separated-emails> [--region <region>]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bucket)  BUCKET="$2";  shift 2 ;;
        --emails)  EMAILS="$2";  shift 2 ;;
        --region)  REGION="$2";  shift 2 ;;
        *) usage ;;
    esac
done

[[ -z "$BUCKET" || -z "$EMAILS" ]] && usage

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "=== Step 1: Create SNS topic ==="
TOPIC_ARN=$(aws sns create-topic \
    --name "$TOPIC_NAME" \
    --region "$REGION" \
    --query TopicArn --output text)
echo "Topic ARN: $TOPIC_ARN"

echo "=== Step 2: Subscribe emails ==="
IFS=',' read -ra EMAIL_LIST <<< "$EMAILS"
for email in "${EMAIL_LIST[@]}"; do
    email=$(echo "$email" | xargs)  # trim whitespace
    aws sns subscribe \
        --topic-arn "$TOPIC_ARN" \
        --protocol email \
        --notification-endpoint "$email" \
        --region "$REGION" > /dev/null
    echo "  Subscribed $email (pending confirmation — check inbox)"
done

echo "=== Step 3: Grant S3 permission to publish to SNS topic ==="
aws sns set-topic-attributes \
    --topic-arn "$TOPIC_ARN" \
    --attribute-name Policy \
    --attribute-value "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [{
            \"Sid\": \"AllowS3Publish\",
            \"Effect\": \"Allow\",
            \"Principal\": {\"Service\": \"s3.amazonaws.com\"},
            \"Action\": \"SNS:Publish\",
            \"Resource\": \"$TOPIC_ARN\",
            \"Condition\": {
                \"ArnLike\": {\"aws:SourceArn\": \"arn:aws:s3:::$BUCKET\"}
            }
        }]
    }" \
    --region "$REGION"
echo "SNS policy updated."

echo "=== Step 4: Add S3 event notification for report uploads ==="

# Fetch existing notification config so we don't overwrite unpack-zips rules.
EXISTING=$(aws s3api get-bucket-notification-configuration \
    --bucket "$BUCKET" \
    --region "$REGION" 2>/dev/null || echo '{}')

# Build the two new SNS configurations (LF reports + RS/researcher reports).
NEW_SNS_CONFIGS=$(cat <<EOF
[
    {
        "Id": "notify-lf-reports",
        "TopicArn": "$TOPIC_ARN",
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {
            "Key": {
                "FilterRules": [
                    {"Name": "prefix", "Value": "academia/reports/"},
                    {"Name": "suffix", "Value": ".json"}
                ]
            }
        }
    },
    {
        "Id": "notify-rs-reports",
        "TopicArn": "$TOPIC_ARN",
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {
            "Key": {
                "FilterRules": [
                    {"Name": "prefix", "Value": "academia/testing/reports/"},
                    {"Name": "suffix", "Value": ".json"}
                ]
            }
        }
    }
]
EOF
)

# Merge new SNS configs with existing Lambda configs.
LAMBDA_CONFIGS=$(echo "$EXISTING" | python3 -c "
import json, sys
data = json.load(sys.stdin)
print(json.dumps(data.get('LambdaFunctionConfigurations', [])))
")

FULL_CONFIG=$(python3 -c "
import json, sys
lambda_cfgs = json.loads('''$LAMBDA_CONFIGS''')
sns_cfgs = json.loads('''$NEW_SNS_CONFIGS''')
print(json.dumps({
    'LambdaFunctionConfigurations': lambda_cfgs,
    'TopicConfigurations': sns_cfgs
}))
")

aws s3api put-bucket-notification-configuration \
    --bucket "$BUCKET" \
    --notification-configuration "$FULL_CONFIG" \
    --region "$REGION"

echo ""
echo "=== Done ==="
echo "S3 bucket '$BUCKET' will now send SNS notifications for:"
echo "  academia/reports/**/*.json         (participant bug reports)"
echo "  academia/testing/reports/**/*.json (researcher bug reports)"
echo ""
echo "Each subscribed email must CONFIRM the subscription via the email AWS just sent."
echo "Notifications will not arrive until confirmed."
