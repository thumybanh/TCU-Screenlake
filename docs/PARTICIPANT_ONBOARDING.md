# Participant Onboarding Guide (Researcher-Facing)

End-to-end process for enrolling a new participant in the Screenlake TCU study.

---

## Overview

Three stages per participant:

1. **Pre-registration** (you, terminal) — reserve a 4-digit code for their email.
2. **Install + signup** (participant, phone) — download APK, sign up with their email + code, confirm via email.
3. **Permission setup** (participant, phone) — enable Accessibility, Usage Access, battery exemption, WiFi upload.

Only after stage 3 does data start flowing to S3.

---

## Stage 1 — Pre-register (you, before the participant arrives)

**You need from the participant:** their email address (any provider — gmail, outlook, yahoo, tcu.edu).

**Reserve their code:**

```bash
assign-code participant@email.com
```

Output:
```
  participant@email.com  ->  0005
```

**Batch (multiple participants):**

```bash
cat > /tmp/batch.txt <<EOF
alice@gmail.com
bob@tcu.edu
carol@yahoo.com
EOF
assign-code --batch /tmp/batch.txt
```

**See all current reservations:**

```bash
assign-code --list
```

`claimed: true` = participant has completed signup. `claimed: false` = waiting.

---

## Stage 2 — Send instructions to participant

Email them the APK download link + their code. Template:

> Hi [Name],
>
> Thanks for joining the TCU Screenlake study. Here's what to do:
>
> **1. Download the app:** [OneDrive link to app-debug.apk]
>
> **2. Install it:** Tap the downloaded file. If Android blocks it, tap "Settings" on the warning, enable "Allow from this source" for your browser, then tap Install.
>
> **3. Open the app and sign up:**
>   - Email: `[their email]`
>   - Participation code: `[their 4-digit code]`
>   - Create a password (at least 8 characters, including upper, lower, number, and symbol)
>
> **4. Confirm your email:** You'll get a verification code at `[their email]`. Check your inbox (and spam folder). Enter the 6-digit code in the app.
>
> **5. Follow the on-screen setup for Accessibility and Usage permissions** (see attached one-pager, or follow the app's prompts).
>
> If anything doesn't work, screenshot the error and reply to this email.

Attach the participant-facing guide: `docs/PARTICIPANT_INSTRUCTIONS.md`.

---

## Stage 3 — Verify data is flowing

After participant finishes setup, verify within 2–4 hours:

**Check they confirmed signup:**

```bash
assign-code --list
```

Their row should show `claimed: true`.

**Check their TCU code was auto-assigned:**

```bash
aws cognito-idp list-users \
  --user-pool-id us-east-2_5pvOLMS20 \
  --filter 'email = "participant@email.com"' \
  --region us-east-2 \
  --query 'Users[0].Attributes[?Name==`custom:tcu_code` || Name==`email`]'
```

**Check their uploads landed in S3:**

```bash
# Replace 0005 with their actual code
aws s3 ls s3://tcu-bucket/academia/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/0005/ \
  --region us-east-2 --recursive | head
```

Should see `image_zip_*.zip` files. If empty after 4+ hours, something in Stage 3 setup failed — see troubleshooting.

**Check their diagnostic logs:**

```bash
aws s3 ls s3://tcu-bucket/academia/log_events_v2/0005/ --region us-east-2
```

If both image zips AND log_events are empty, the service never ran (permissions issue). If logs exist but no image zips, upload is gated (WiFi/power not met) or still catching up.

---

## Troubleshooting

### Participant doesn't receive confirmation email

- Check spam folder first.
- Verify the exact email they typed matches what you pre-registered:
  ```bash
  aws cognito-idp list-users --user-pool-id us-east-2_5pvOLMS20 \
    --region us-east-2 --query 'Users[].Attributes[?Name==`email`].Value'
  ```
- Resend code:
  ```bash
  aws cognito-idp resend-confirmation-code \
    --client-id 2b3ailgqgai8diph2rka06ns2a \
    --username participant@email.com \
    --region us-east-2
  ```
- Known issue: Cognito default sender has 50 emails/day limit and spam-filter issues. For >50 participants/day, switch to SES.

### Signup rejected: "email not on participant list"

They typed a different email than you pre-registered (typo, dot variations, wrong domain). Confirm with them, re-pre-register their actual email.

### Signup rejected: "code does not match"

They typed the wrong code. Confirm their assigned code in `assign-code --list`, have them retry.

### Signup succeeded but no S3 data after 4+ hours

Participant's phone permission setup is incomplete. Most common on Android 13+ (Pixel, modern Samsung):
- Accessibility permission blocked by "restricted settings" (sideload safeguard). Must go to Settings → Apps → Screenlake → ⋮ menu → Allow restricted settings, then enable Accessibility.
- Battery optimization killing the service in background.
- Default upload rules require WiFi + charging — if they never charge on WiFi, nothing uploads.

See `PARTICIPANT_INSTRUCTIONS.md` troubleshooting section.

### Participant signed up BEFORE pre-registration existed

If PostConfirmation runs before `pre#<email>` is written, participant gets an auto-assigned 1001+ code instead of the 4-digit pre-reserved one. S3 uploads go under that auto-assigned code.

Example (`jessie.wangkexin@gmail.com` has reservation 0001 but TCU code 1002):

```bash
aws dynamodb delete-item --table-name screenlake-tcu-codes \
  --key '{"id":{"S":"pre#jessie.wangkexin@gmail.com"}}' --region us-east-2
aws dynamodb delete-item --table-name screenlake-tcu-codes \
  --key '{"id":{"S":"code#0001"}}' --region us-east-2
# Update spreadsheet: Jessie is 1002, not 0001.
```

### Need to delete a participant entirely

```bash
# Delete Cognito account (triggers signout on their phone)
aws cognito-idp admin-delete-user \
  --user-pool-id us-east-2_5pvOLMS20 \
  --username participant@email.com --region us-east-2

# Delete DynamoDB records (replace <sub> and <code> with real values)
aws dynamodb delete-item --table-name screenlake-tcu-codes \
  --key '{"id":{"S":"user#<sub>"}}' --region us-east-2
aws dynamodb delete-item --table-name screenlake-tcu-codes \
  --key '{"id":{"S":"code#<code>"}}' --region us-east-2
aws dynamodb delete-item --table-name screenlake-tcu-codes \
  --key '{"id":{"S":"pre#participant@email.com"}}' --region us-east-2

# S3 data stays until manually deleted:
aws s3 rm s3://tcu-bucket/academia/tenant/general_tenant_1_general_tenant/panel/general_1/V_12/panelist/<code>/ \
  --recursive --region us-east-2
```

---

## Reference

- **AWS region:** `us-east-2`
- **S3 bucket:** `tcu-bucket`
- **Cognito User Pool:** `us-east-2_5pvOLMS20`
- **Cognito App Client:** `2b3ailgqgai8diph2rka06ns2a`
- **DynamoDB table:** `screenlake-tcu-codes`
- **Lambda functions:** `screenlake-assign-tcu` (PreSignUp + PostConfirmation), `screenlake-unpack-zips` (S3 trigger)
- **Pre-registration code range:** `0001–0999` (researcher-assigned)
- **Auto-assigned code range:** `1001+` (fallback, for participants who signed up without pre-registration)
