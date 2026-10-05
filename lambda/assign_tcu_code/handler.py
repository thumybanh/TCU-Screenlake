"""Lambda: assign a unique 4-digit TCU participant code to a Cognito user.

Called by the Android app right after Cognito signup succeeds. Returns the
code that identifies the participant everywhere in the pipeline (S3
`panelist=<code>/` path, `_index.json`, `data/` tree).

Idempotency: if the same Cognito user calls again (reinstall on new device,
retry after network hiccup), returns the code they already have. Never
double-assigns.

Uniqueness: DynamoDB atomic counter guarantees sequential, collision-free
assignment. Starts at 1001. Skips codes already used by pre-existing test
data (1234, 2323) or manually pre-populated in the table.

Invocation shape:
    {"cognito_sub": "<sub>", "cognito_username": "<username>"}

Response shape:
    {"tcu_code": "1005", "assigned_now": false, "cognito_sub": "<sub>"}

Failure modes raise HandlerError → Lambda returns 500 to the app, which
should surface a "signup incomplete, please try again" message to the
participant.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TCU_TABLE_NAME", "screenlake-tcu-codes")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
cognito_idp = boto3.client("cognito-idp")


class HandlerError(RuntimeError):
    pass


def lambda_handler(event, _context):
    # Cognito PostConfirmation trigger shape
    if "request" in event and "userAttributes" in event.get("request", {}):
        attrs = event["request"]["userAttributes"]
        cognito_sub = attrs.get("sub")
        cognito_username = attrs.get("email") or event.get("userName") or ""
    else:
        cognito_sub = _extract(event, "cognito_sub")
        cognito_username = _extract(event, "cognito_username") or ""

    existing = _lookup_existing(cognito_sub)
    if existing is not None:
        # Best-effort re-sync of the Cognito attribute in case it was cleared or
        # never written on a prior run. Idempotent from the app's perspective.
        _write_cognito_attribute(cognito_username, existing)
        return _ok(existing, cognito_sub, assigned_now=False)

    tcu_code = _assign_new(cognito_sub, cognito_username)
    _write_cognito_attribute(cognito_username, tcu_code)
    return _ok(tcu_code, cognito_sub, assigned_now=True)


def _extract(event, key: str) -> Optional[str]:
    # API Gateway wraps under 'body' as JSON string; direct Lambda invoke
    # passes the payload as-is. Handle both.
    if key in event:
        return event[key]
    body = event.get("body")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return None
        return parsed.get(key)
    if isinstance(body, dict):
        return body.get(key)
    return None


def _lookup_existing(cognito_sub: str) -> Optional[str]:
    if not cognito_sub:
        raise HandlerError("missing cognito_sub")
    resp = table.get_item(Key={"id": f"user#{cognito_sub}"})
    item = resp.get("Item")
    if item is None:
        return None
    return item.get("tcu_code")


def _assign_new(cognito_sub: str, cognito_username: str) -> str:
    for _ in range(9000):  # bounded so we don't spin forever if code space fills
        next_code = _bump_counter()
        formatted = f"{next_code:04d}"

        if _is_reserved(formatted):
            continue

        # Try to persist the mapping. Use a conditional write so a race with
        # another concurrent invocation (extremely rare given DynamoDB atomic
        # counter, but possible if the counter was manually mutated) fails
        # loudly instead of overwriting.
        try:
            table.put_item(
                Item={
                    "id": f"user#{cognito_sub}",
                    "tcu_code": formatted,
                    "cognito_username": cognito_username,
                    "assigned_at_epoch": _now_epoch(),
                },
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                # Another concurrent invocation for the same user already wrote
                # the mapping. Read whichever code won and return it.
                existing = _lookup_existing(cognito_sub)
                if existing is not None:
                    return existing
                raise HandlerError(f"race condition writing user mapping for {cognito_sub}")
            raise

        # Reverse mapping so we can enumerate assigned codes without a scan.
        table.put_item(
            Item={
                "id": f"code#{formatted}",
                "cognito_sub": cognito_sub,
                "cognito_username": cognito_username,
                "assigned_at_epoch": _now_epoch(),
            }
        )
        return formatted

    raise HandlerError("code space exhausted or too many collisions with reserved codes")


def _bump_counter() -> int:
    resp = table.update_item(
        Key={"id": "counter"},
        UpdateExpression="SET last_assigned = if_not_exists(last_assigned, :start) + :incr",
        ExpressionAttributeValues={":start": 1000, ":incr": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["last_assigned"])


def _is_reserved(code: str) -> bool:
    resp = table.get_item(Key={"id": f"code#{code}"})
    return resp.get("Item") is not None


def _now_epoch() -> int:
    import time
    return int(time.time())


def _ok(tcu_code: str, cognito_sub: str, assigned_now: bool) -> dict:
    return {
        "tcu_code": tcu_code,
        "assigned_now": assigned_now,
        "cognito_sub": cognito_sub,
    }


def _write_cognito_attribute(cognito_username: str, tcu_code: str) -> None:
    """Write the assigned code to the Cognito user's custom:tcu_code attribute.
    Best-effort: failures here are logged but do not fail the Lambda, because
    the DynamoDB record is the source of truth for uniqueness. Mobile app
    reads this attribute on launch to skip the invite-code screen.
    """
    if not COGNITO_USER_POOL_ID or not cognito_username:
        return
    try:
        cognito_idp.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=[
                {"Name": "custom:tcu_code", "Value": tcu_code},
            ],
        )
    except ClientError as e:
        # Log and swallow — do not fail the assignment. Attribute can be
        # re-synced by re-invoking the Lambda for the same user later.
        code = e.response.get("Error", {}).get("Code", "Unknown")
        msg = e.response.get("Error", {}).get("Message", str(e))
        print(f"warning: could not write custom:tcu_code for {cognito_username}: {code} {msg}")
