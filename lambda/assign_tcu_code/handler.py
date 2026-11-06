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
import random
from typing import Optional

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TCU_TABLE_NAME", "screenlake-tcu-codes")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")

# Role definitions:
#   TCU  = real study participant. Random code in 1000-9999 (no leading zero).
#   TEST = researcher mock account. Random code in 0001-0999 (has leading zero).
# The role is the source of truth for data routing (S3 prefix, Athena queries).
# A TEST account whose code happens to fall in the TCU range is still TEST —
# role, not code value, drives filtering.
ROLE_TCU = "TCU"
ROLE_TEST = "TEST"
CODE_RANGES = {
    ROLE_TCU: (1000, 9999),
    ROLE_TEST: (1, 999),
}
RANDOM_RETRY_LIMIT = 50

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
cognito_idp = boto3.client("cognito-idp")


class HandlerError(RuntimeError):
    pass


def lambda_handler(event, _context):
    # Cognito triggers (PreSignUp, PostConfirmation) share the same event shape.
    # Dispatch by triggerSource so one Lambda handles both.
    trigger_source = event.get("triggerSource", "")

    if trigger_source == "PreSignUp_SignUp":
        return _handle_pre_signup(event)

    # Admin-created users (AdminCreateUser) and external-provider sign-ups skip
    # the participant-code validation. The researcher is trusted to pre-reserve
    # the code separately and the PostConfirmation trigger will still assign
    # the TCU code via the email-lookup fallback.
    if trigger_source in ("PreSignUp_AdminCreateUser", "PreSignUp_ExternalProvider"):
        return event

    # Cognito PostConfirmation trigger shape — must always return the event
    # unchanged so Cognito finishes signup, even if assignment fails.
    is_cognito_trigger = "request" in event and "userAttributes" in event.get("request", {})

    # Explicit researcher-tool actions (not from Cognito trigger)
    action = _extract(event, "action") if not is_cognito_trigger else None
    if action == "pre_reserve":
        email = (_extract(event, "email") or "").strip().lower()
        if not email:
            raise HandlerError("missing email")
        role = _normalize_role(_extract(event, "role"))
        code = _pre_reserve_code(email, role)
        return {"email": email, "tcu_code": code, "role": role, "pre_reserved": True}

    if action == "list_pre_reservations":
        return {"reservations": _list_pre_reservations()}

    if action == "validate_code":
        email = (_extract(event, "email") or "").strip().lower()
        code = (_extract(event, "code") or "").strip()
        if not email or not code:
            return {"ok": False, "reason": "missing_email_or_code"}
        reservation = _lookup_pre_reservation(email)
        if reservation is None:
            return {"ok": False, "reason": "email_not_registered"}
        if reservation != code:
            return {"ok": False, "reason": "code_mismatch"}
        return {"ok": True}

    if is_cognito_trigger:
        attrs = event["request"]["userAttributes"]
        cognito_sub = attrs.get("sub")
        cognito_username = event.get("userName") or attrs.get("email") or ""
        email = (attrs.get("email") or "").strip().lower()
    else:
        cognito_sub = _extract(event, "cognito_sub")
        cognito_username = _extract(event, "cognito_username") or ""
        email = (_extract(event, "email") or "").strip().lower()

    existing_user = _lookup_existing_user(cognito_sub)
    if existing_user is not None:
        existing_code = existing_user.get("tcu_code")
        existing_role = existing_user.get("role") or ROLE_TCU
        # Best-effort re-sync of the Cognito attributes in case they were
        # cleared or never written on a prior run. Idempotent from the app's
        # perspective.
        _write_cognito_attributes(cognito_username, existing_code, existing_role)
        if is_cognito_trigger:
            return event
        return _ok(existing_code, cognito_sub, existing_role, assigned_now=False)

    # Pre-reserved code lookup. If researcher pre-assigned a code for this
    # email, honor it. Otherwise fall through to auto-assign as a TCU participant.
    pre = _lookup_pre_reservation_full(email) if email else None
    if pre is not None:
        pre_code = pre["tcu_code"]
        pre_role = pre.get("role") or ROLE_TCU
        _claim_pre_reservation(cognito_sub, cognito_username, email, pre_code, pre_role)
        _write_cognito_attributes(cognito_username, pre_code, pre_role)
        if is_cognito_trigger:
            return event
        return _ok(pre_code, cognito_sub, pre_role, assigned_now=True)

    # No pre-reservation: default to TCU role with a random code.
    tcu_code = _assign_new(cognito_sub, cognito_username, ROLE_TCU)
    _write_cognito_attributes(cognito_username, tcu_code, ROLE_TCU)
    if is_cognito_trigger:
        return event
    return _ok(tcu_code, cognito_sub, ROLE_TCU, assigned_now=True)


def _handle_pre_signup(event: dict) -> dict:
    """Cognito PreSignUp trigger: validate the typed participant code against
    the researcher's pre-reservation for this email. Reject the signup if the
    code does not match. Allow the signup if it matches; the PostConfirmation
    trigger will claim the reservation and assign the TCU code.

    Rejecting a signup from a PreSignUp trigger is done by raising an
    exception — Cognito surfaces the exception's message string back to the
    app as the signup error. Keep messages short and participant-friendly.
    """
    attrs = event.get("request", {}).get("userAttributes", {}) or {}
    email = (attrs.get("email") or "").strip().lower()

    client_metadata = event.get("request", {}).get("clientMetadata", {}) or {}
    typed_code = (client_metadata.get("panel_code") or "").strip()

    if not email:
        raise HandlerError("Email is required for signup.")

    if not typed_code:
        raise HandlerError(
            "Missing participation code. Please re-enter the 4-digit code "
            "your researcher emailed you."
        )

    reservation = _lookup_pre_reservation(email)
    if reservation is None:
        raise HandlerError(
            "This email is not on the participant list. Make sure you entered "
            "the exact email your researcher used, or contact your researcher."
        )

    if reservation != typed_code:
        raise HandlerError(
            "The participation code does not match this email. Please check "
            "the code your researcher emailed you and try again."
        )

    # Code matches — allow signup to proceed. Email auto-verification stays as
    # Cognito pool default (we do not set autoConfirmUser or autoVerifyEmail).
    return event


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


def _normalize_role(role_in) -> str:
    """Normalize the role field from caller input. Default TCU (real participant)
    if missing or unrecognized — safer than silently assigning the TEST path."""
    if not role_in:
        return ROLE_TCU
    role = str(role_in).strip().upper()
    if role in (ROLE_TCU, ROLE_TEST):
        return role
    return ROLE_TCU


def _random_code(role: str) -> str:
    """Pick a random 4-digit code in the role's range, formatted with leading
    zeros if needed. Caller must separately check for collisions."""
    start, end = CODE_RANGES[role]
    return f"{random.randint(start, end):04d}"


def _lookup_existing_user(cognito_sub: str) -> Optional[dict]:
    if not cognito_sub:
        raise HandlerError("missing cognito_sub")
    resp = table.get_item(Key={"id": f"user#{cognito_sub}"})
    return resp.get("Item")


def _assign_new(cognito_sub: str, cognito_username: str, role: str) -> str:
    """Pick a random code in the role's range, verify not taken, persist with
    a conditional write. Retries on collision up to RANDOM_RETRY_LIMIT."""
    for _ in range(RANDOM_RETRY_LIMIT):
        formatted = _random_code(role)

        if _is_reserved(formatted):
            continue

        try:
            table.put_item(
                Item={
                    "id": f"user#{cognito_sub}",
                    "tcu_code": formatted,
                    "role": role,
                    "cognito_username": cognito_username,
                    "assigned_at_epoch": _now_epoch(),
                },
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = _lookup_existing_user(cognito_sub)
                if existing is not None:
                    return existing.get("tcu_code")
                raise HandlerError(f"race condition writing user mapping for {cognito_sub}")
            raise

        table.put_item(
            Item={
                "id": f"code#{formatted}",
                "cognito_sub": cognito_sub,
                "cognito_username": cognito_username,
                "role": role,
                "assigned_at_epoch": _now_epoch(),
            }
        )
        return formatted

    raise HandlerError(f"random code collisions exceeded retry limit for role {role}")


def _pre_reserve_code(email: str, role: str) -> str:
    """Reserve a random 4-digit code for a participant email.
    Idempotent: if the email already has a reservation, return the existing one."""
    existing = _lookup_pre_reservation(email)
    if existing is not None:
        return existing

    for _ in range(RANDOM_RETRY_LIMIT):
        formatted = _random_code(role)

        if _is_reserved(formatted):
            continue

        try:
            table.put_item(
                Item={
                    "id": f"pre#{email}",
                    "tcu_code": formatted,
                    "email": email,
                    "role": role,
                    "reserved_at_epoch": _now_epoch(),
                    "claimed": False,
                },
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                existing = _lookup_pre_reservation(email)
                if existing is not None:
                    return existing
                raise HandlerError(f"race condition writing pre-reservation for {email}")
            raise

        # Reverse mapping so this code is marked used.
        table.put_item(
            Item={
                "id": f"code#{formatted}",
                "email": email,
                "role": role,
                "pre_reserved": True,
                "reserved_at_epoch": _now_epoch(),
            }
        )
        return formatted

    raise HandlerError(f"pre-reservation collisions exceeded retry limit for role {role}")


def _lookup_pre_reservation(email: str) -> Optional[str]:
    if not email:
        return None
    resp = table.get_item(Key={"id": f"pre#{email}"})
    item = resp.get("Item")
    if item is None:
        return None
    return item.get("tcu_code")


def _lookup_pre_reservation_full(email: str) -> Optional[dict]:
    """Return the full pre-reservation record including role, not just the code."""
    if not email:
        return None
    resp = table.get_item(Key={"id": f"pre#{email}"})
    return resp.get("Item")


def _claim_pre_reservation(cognito_sub: str, cognito_username: str, email: str, tcu_code: str, role: str) -> None:
    """Participant has signed up with their pre-assigned email. Write the
    user#sub record binding their Cognito identity to the pre-assigned code
    and role, and mark the reservation as claimed."""
    try:
        table.put_item(
            Item={
                "id": f"user#{cognito_sub}",
                "tcu_code": tcu_code,
                "role": role,
                "cognito_username": cognito_username,
                "email": email,
                "assigned_at_epoch": _now_epoch(),
                "from_pre_reservation": True,
            },
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return  # already claimed, idempotent
        raise

    # Mark reservation as claimed (best-effort; failure here is non-fatal).
    try:
        table.update_item(
            Key={"id": f"pre#{email}"},
            UpdateExpression="SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
            ExpressionAttributeValues={
                ":t": True,
                ":s": cognito_sub,
                ":e": _now_epoch(),
            },
        )
    except ClientError:
        pass


def _list_pre_reservations() -> list:
    """Scan all pre-reservation records. Used by researcher to audit assignments."""
    items = []
    resp = table.scan(
        FilterExpression="begins_with(id, :p)",
        ExpressionAttributeValues={":p": "pre#"},
    )
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(
            FilterExpression="begins_with(id, :p)",
            ExpressionAttributeValues={":p": "pre#"},
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return [
        {
            "email": it.get("email"),
            "tcu_code": it.get("tcu_code"),
            "role": it.get("role") or ROLE_TCU,
            "claimed": it.get("claimed", False),
            "reserved_at_epoch": int(it.get("reserved_at_epoch", 0)),
        }
        for it in items
    ]


def _is_reserved(code: str) -> bool:
    resp = table.get_item(Key={"id": f"code#{code}"})
    return resp.get("Item") is not None


def _now_epoch() -> int:
    import time
    return int(time.time())


def _ok(tcu_code: str, cognito_sub: str, role: str, assigned_now: bool) -> dict:
    return {
        "tcu_code": tcu_code,
        "role": role,
        "assigned_now": assigned_now,
        "cognito_sub": cognito_sub,
    }


def _write_cognito_attributes(cognito_username: str, tcu_code: str, role: str) -> None:
    """Write the assigned code and role to the Cognito user's custom attributes.
    Best-effort: failures here are logged but do not fail the Lambda, because
    the DynamoDB record is the source of truth for uniqueness. Mobile app
    reads these attributes on launch to skip the invite-code screen and to
    route uploads to the correct S3 prefix.
    """
    if not COGNITO_USER_POOL_ID or not cognito_username:
        return
    try:
        cognito_idp.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=[
                {"Name": "custom:tcu_code", "Value": tcu_code},
                {"Name": "custom:tcu_role", "Value": role},
            ],
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        msg = e.response.get("Error", {}).get("Message", str(e))
        print(f"warning: could not write Cognito attributes for {cognito_username}: {code} {msg}")
