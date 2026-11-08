#!/usr/bin/env bash
# Phase B: deploy updated handler to screenlake-assign-tcu.
# Scope: screenlake-assign-tcu ONLY.
# No DynamoDB writes, Cognito changes, S3 changes, or screenlake-unpack-zips modifications.
# Run each section in order. Each section exits non-zero on failure — stop immediately.
set -euo pipefail

FUNCTION="screenlake-assign-tcu"
REGION="us-east-2"
HANDLER_SRC="lambda/assign_tcu_code/handler.py"
export FUNCTION_ZIP="lambda/assign_tcu_code/function.zip"
export ROLLBACK_ZIP="lambda/assign_tcu_code/rollback_handler_pre_phase_b.zip"

# ==============================================================================
# B0 — BACKUP AND BASELINE
# Capture RevisionId + CodeSha256. Download and verify current deployment package.
# Stop if download is corrupt. All later steps compare against these values.
# ==============================================================================
echo "=== B0: Capturing baseline ==="

BASELINE_JSON=$(aws lambda get-function-configuration \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --query '{CodeSha256:CodeSha256,RevisionId:RevisionId}' \
  --output json)
echo "Baseline: $BASELINE_JSON"

export BASELINE_SHA=$(echo "$BASELINE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['CodeSha256'])")
export BASELINE_REV=$(echo "$BASELINE_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['RevisionId'])")

echo "Baseline CodeSha256 : $BASELINE_SHA"
echo "Baseline RevisionId : $BASELINE_REV"

BACKUP_URL=$(aws lambda get-function \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --query 'Code.Location' \
  --output text)

curl -fL -o "$ROLLBACK_ZIP" "$BACKUP_URL"
echo "Downloaded rollback ZIP: $ROLLBACK_ZIP"

# Verify download: compare SHA-256 of file against AWS-reported CodeSha256.
# Uses Python for base64 decoding and hashing — works on macOS without xxd or sha256sum.
python3 - <<'EOF'
import base64, hashlib, os, sys

backup_path  = os.environ["ROLLBACK_ZIP"]
expected_b64 = os.environ["BASELINE_SHA"]

with open(backup_path, "rb") as f:
    actual_hex = hashlib.sha256(f.read()).hexdigest()

expected_hex = base64.b64decode(expected_b64).hex()

print(f"Expected SHA-256 (AWS): {expected_hex}")
print(f"Actual   SHA-256 (file): {actual_hex}")

if actual_hex != expected_hex:
    print("ERROR: SHA-256 mismatch — backup corrupt or incomplete. Aborting.")
    sys.exit(1)
print("Backup verified.")
EOF

echo "--- B0 complete ---"
echo ""

# ==============================================================================
# B1 — PACKAGE FROM SCRATCH
# Delete any existing ZIP first so no stale files carry over.
# Verify the archive contains exactly one entry named handler.py and passes
# integrity check before attempting deployment.
# ==============================================================================
echo "=== B1: Packaging handler.py ==="

rm -f "$FUNCTION_ZIP"
zip -j "$FUNCTION_ZIP" "$HANDLER_SRC"

echo "ZIP contents:"
unzip -l "$FUNCTION_ZIP"

# Verify archive integrity
echo "Verifying archive integrity..."
unzip -t "$FUNCTION_ZIP"

# Confirm exactly one entry named handler.py — no stale files, no directory prefix
python3 - <<'EOF'
import zipfile, os, sys

zip_path = os.environ["FUNCTION_ZIP"]
with zipfile.ZipFile(zip_path) as zf:
    names = zf.namelist()

if names != ["handler.py"]:
    print(f"ERROR: expected exactly ['handler.py'] in ZIP, got {names}")
    sys.exit(1)
print(f"Packaging check passed: ZIP contains exactly one entry: {names[0]}")
EOF

echo "--- B1 complete ---"
echo ""

# ==============================================================================
# B2 — PRE-DEPLOY BASELINE CHECK
# Re-fetch CodeSha256 and RevisionId immediately before deploying.
# If either has changed since B0, another deployment occurred — abort.
# ==============================================================================
echo "=== B2: Pre-deploy baseline check ==="

PREDEPLOY_JSON=$(aws lambda get-function-configuration \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --query '{CodeSha256:CodeSha256,RevisionId:RevisionId}' \
  --output json)
echo "Live state: $PREDEPLOY_JSON"

export LIVE_SHA=$(echo "$PREDEPLOY_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['CodeSha256'])")
export LIVE_REV=$(echo "$PREDEPLOY_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['RevisionId'])")

python3 - <<'EOF'
import os, sys

baseline_sha = os.environ["BASELINE_SHA"]
baseline_rev = os.environ["BASELINE_REV"]
live_sha     = os.environ["LIVE_SHA"]
live_rev     = os.environ["LIVE_REV"]

ok = True
if live_sha != baseline_sha:
    print(f"ERROR: CodeSha256 changed since B0 backup.")
    print(f"  Baseline : {baseline_sha}")
    print(f"  Live     : {live_sha}")
    ok = False
if live_rev != baseline_rev:
    print(f"ERROR: RevisionId changed since B0 — another deployment occurred.")
    print(f"  Baseline : {baseline_rev}")
    print(f"  Live     : {live_rev}")
    ok = False
if not ok:
    print("Aborting. Investigate before deploying.")
    sys.exit(1)
print("Pre-deploy check passed. RevisionId and CodeSha256 unchanged since B0.")
EOF

echo "--- B2 complete ---"
echo ""

# ==============================================================================
# B3 — DEPLOY
# --revision-id guard: fails with PreconditionFailedException if another
# deployment occurred between B2 and B3, preventing silent overwrites.
# ==============================================================================
echo "=== B3: Deploying to $FUNCTION ==="

aws lambda update-function-code \
  --function-name "$FUNCTION" \
  --zip-file "fileb://$FUNCTION_ZIP" \
  --revision-id "$LIVE_REV" \
  --region "$REGION"

echo "--- B3 complete ---"
echo ""

# ==============================================================================
# B4 — WAIT AND CONFIRM
# Verify LastUpdateStatus is Successful and CodeSha256 has changed.
# Record the new CodeSha256 — used by rollback to confirm it is not overwriting
# a different deployment.
# ==============================================================================
echo "=== B4: Waiting for update to propagate ==="

aws lambda wait function-updated \
  --function-name "$FUNCTION" \
  --region "$REGION"

export POSTDEPLOY_JSON=$(aws lambda get-function-configuration \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --query '{LastUpdateStatus:LastUpdateStatus,CodeSha256:CodeSha256,LastModified:LastModified}' \
  --output json)
echo "Post-deploy state: $POSTDEPLOY_JSON"

export DEPLOYED_SHA=$(echo "$POSTDEPLOY_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['CodeSha256'])")

python3 - <<'EOF'
import os, sys

baseline_sha  = os.environ["BASELINE_SHA"]
deployed_sha  = os.environ["DEPLOYED_SHA"]
postdeploy    = os.environ["POSTDEPLOY_JSON"]

import json
d = json.loads(postdeploy)
status = d.get("LastUpdateStatus", "")

if status != "Successful":
    print(f"FAIL: LastUpdateStatus='{status}', expected 'Successful'")
    sys.exit(1)
if deployed_sha == baseline_sha:
    print("FAIL: CodeSha256 unchanged after deployment — new code may not have deployed")
    sys.exit(1)

print(f"Deployment confirmed.")
print(f"  LastUpdateStatus : {status}")
print(f"  New CodeSha256   : {deployed_sha}")
print(f"  LastModified     : {d.get('LastModified')}")
EOF

echo "--- B4 complete ---"
echo ""

# ==============================================================================
# B5 — SMOKE TEST 1: list_cohort (read-only)
# list_cohort calls table.scan() only — no writes at any code path.
# Expected: no FunctionError; payload contains cohort_id and enrollments list.
# ==============================================================================
echo "=== B5: Smoke test — list_cohort ==="

aws lambda invoke \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --payload '{"action":"list_cohort","cohort_id":"general_1","caller_identity":"smoke-test-b"}' \
  --cli-binary-format raw-in-base64-out \
  --output json \
  /tmp/smoke_list.json > /tmp/smoke_list_meta.json

echo "Metadata:"; cat /tmp/smoke_list_meta.json
echo ""; echo "Payload:"; cat /tmp/smoke_list.json; echo ""

python3 - <<'EOF'
import json, sys

with open('/tmp/smoke_list_meta.json') as f:
    meta = json.load(f)
with open('/tmp/smoke_list.json') as f:
    payload = json.load(f)

func_err = meta.get('FunctionError', '')
if func_err:
    print(f"FAIL: FunctionError={func_err}")
    print(f"Payload: {payload}")
    sys.exit(1)

if 'cohort_id' not in payload:
    print(f"FAIL: missing 'cohort_id' in response: {payload}")
    sys.exit(1)
if 'enrollments' not in payload:
    print(f"FAIL: missing 'enrollments' in response: {payload}")
    sys.exit(1)
if not isinstance(payload['enrollments'], list):
    print(f"FAIL: 'enrollments' is not a list: {payload}")
    sys.exit(1)

print(f"PASS: list_cohort returned cohort_id='{payload['cohort_id']}', "
      f"{len(payload['enrollments'])} enrollment(s)")
EOF

echo "--- B5 complete ---"
echo ""

# ==============================================================================
# B6 — SMOKE TEST 2: migrate_researcher missing fields (expected rejection)
# Validation at dispatcher line 167 raises HandlerError BEFORE
# _migrate_researcher_account is called — no DynamoDB reads, no Cognito
# contact, no account data touched.
#
# Expected invocation metadata: FunctionError="Unhandled"
# Expected payload: errorType="HandlerError", errorMessage contains
#   "migrate_researcher requires"
#
# Distinguishes clean validation rejection (HandlerError) from an unexpected
# dispatcher crash (any other errorType).
# ==============================================================================
echo "=== B6: Smoke test — migrate_researcher missing-fields rejection ==="

aws lambda invoke \
  --function-name "$FUNCTION" \
  --region "$REGION" \
  --payload '{"action":"migrate_researcher","caller_identity":"smoke-test-b"}' \
  --cli-binary-format raw-in-base64-out \
  --output json \
  /tmp/smoke_migrate.json > /tmp/smoke_migrate_meta.json

echo "Metadata:"; cat /tmp/smoke_migrate_meta.json
echo ""; echo "Payload:"; cat /tmp/smoke_migrate.json; echo ""

python3 - <<'EOF'
import json, sys

with open('/tmp/smoke_migrate_meta.json') as f:
    meta = json.load(f)
with open('/tmp/smoke_migrate.json') as f:
    payload = json.load(f)

func_err   = meta.get('FunctionError', '')
error_type = payload.get('errorType', '')
error_msg  = payload.get('errorMessage', '')

if func_err != 'Unhandled':
    print(f"FAIL: expected FunctionError='Unhandled', got '{func_err}'")
    print(f"  If empty: migrate_researcher action is not wired in the deployed handler.")
    sys.exit(1)

if error_type != 'HandlerError':
    print(f"FAIL: expected errorType='HandlerError', got '{error_type}'")
    print(f"  A different errorType indicates a dispatcher crash, not a clean rejection.")
    print(f"  Full payload: {payload}")
    sys.exit(1)

if 'migrate_researcher requires' not in error_msg:
    print(f"FAIL: unexpected errorMessage: '{error_msg}'")
    sys.exit(1)

print(f"PASS: migrate_researcher correctly rejected missing fields.")
print(f"  FunctionError : {func_err}")
print(f"  errorType     : {error_type}")
print(f"  errorMessage  : {error_msg}")
EOF

echo "--- B6 complete ---"
echo ""
echo "=== Phase B complete. All checks passed. ==="
echo ""
echo "Rollback ZIP retained at: $ROLLBACK_ZIP"
echo "Deployed CodeSha256     : $DEPLOYED_SHA"
echo ""
echo "Next: Phase C (researcher migration) requires separate approval."

# ==============================================================================
# ROLLBACK — restore pre-Phase-B deployment
# Run only if needed. Does NOT run automatically as part of Phase B.
#
# Safety checks before writing:
#   1. Live CodeSha256 must match what was recorded in B4 (DEPLOYED_SHA).
#      If it differs, another deployment occurred — abort to avoid overwrite.
#   2. RevisionId guard on update-function-code — same protection as B3.
#
# Restores exact pre-Phase-B bytes. Changes only screenlake-assign-tcu
# function code. No DynamoDB, Cognito, S3, or screenlake-unpack-zips contact.
# ==============================================================================
# ROLLBACK (run manually only if needed):
#
#   ROLLBACK_CHECK=$(aws lambda get-function-configuration \
#     --function-name screenlake-assign-tcu \
#     --region us-east-2 \
#     --query '{CodeSha256:CodeSha256,RevisionId:RevisionId}' \
#     --output json)
#
#   python3 - <<'PYEOF'
#   import json, os, sys
#   d = json.loads(os.popen("echo '$ROLLBACK_CHECK'").read())
#   # Verify live SHA matches what was deployed in B4 before we overwrite it
#   deployed_sha = os.environ.get("DEPLOYED_SHA", "")
#   live_sha     = d["CodeSha256"]
#   live_rev     = d["RevisionId"]
#   if deployed_sha and live_sha != deployed_sha:
#       print(f"ERROR: live CodeSha256 ({live_sha}) != Phase B deployed SHA ({deployed_sha})")
#       print("Another deployment occurred. Investigate before rolling back.")
#       sys.exit(1)
#   print(f"Rollback pre-check passed. RevisionId={live_rev}")
#   PYEOF
#
#   ROLLBACK_REV=$(aws lambda get-function-configuration \
#     --function-name screenlake-assign-tcu \
#     --region us-east-2 \
#     --query 'RevisionId' --output text)
#
#   aws lambda update-function-code \
#     --function-name screenlake-assign-tcu \
#     --zip-file fileb://lambda/assign_tcu_code/rollback_handler_pre_phase_b.zip \
#     --revision-id "$ROLLBACK_REV" \
#     --region us-east-2 \
#   && aws lambda wait function-updated \
#     --function-name screenlake-assign-tcu \
#     --region us-east-2 \
#   && echo "Rollback complete."
