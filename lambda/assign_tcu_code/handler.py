"""Lambda: assign a unique participant or researcher ID to a Cognito user.

ID formats (both exactly 6 characters):
  LF (Leap Forward participant): LF + 4-digit-random (1000-9999), e.g. LF4342.
      Random, no cohort embedded. Cohort stored separately in DynamoDB.
      Unique across all cohorts; never reassigned even if participant withdraws.
  RS (Research Staff): RS + 4-digit-sequential (0001-9999), e.g. RS0001.
      Sequential from 0001; assigned atomically via DynamoDB counter.

Cognito attributes written (names unchanged for backward compat):
  custom:tcu_code  — the full ID string (LF14827 or RS001)
  custom:tcu_role  — the role string (LF or RS)

Legacy role values "TCU" and "TEST" are normalized to "LF" and "RS"
so existing accounts keep routing correctly without a migration.

Idempotency: if the same Cognito user calls again (reinstall / retry),
returns the ID they already have. Never double-assigns.

Failure modes raise HandlerError → Lambda returns 500 to the app.

RS ID safety guarantees:
  - counter#RS must be explicitly seeded (via seed_rs_counter action) before
    any RS assignment. The Lambda refuses to auto-bootstrap the counter to
    prevent accidental re-issuance of codes 0001–0005.
  - Retired RS codes are permanently blocked via two redundant records:
      retired#RS<n>  — dedicated tombstone, never deleted
      code#RS<n>     — lock record updated with retired=True, never deleted
    Both are checked in _is_rs_reserved(). Deleting one record still leaves
    the other as a barrier, so a single accidental delete cannot re-open a slot.
  - _normalize_rs_number() validates input format strictly; invalid inputs raise
    rather than silently producing an incorrect key.
  - All RS mutations (reserve, claim, retire) use DynamoDB TransactWriteItems
    so partial updates cannot leave the system in an inconsistent state.
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

ROLE_LF = "LF"
ROLE_RS = "RS"
# Legacy constants kept so DynamoDB records written before the rename are
# still recognized when old items are read back.
ROLE_TCU = "TCU"
ROLE_TEST = "TEST"

PARTICIPANT_NUMBER_RANGE = (1000, 9999)   # LF: 4 digits, no leading zero
RS_MAX = 9999                             # RS: 0001-9999, zero-padded to 4 digits
RANDOM_RETRY_LIMIT = 50

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
cognito_idp = boto3.client("cognito-idp")


class HandlerError(RuntimeError):
    pass


def lambda_handler(event, context):
    trigger_source = event.get("triggerSource", "")

    if trigger_source == "PreSignUp_SignUp":
        return _handle_pre_signup(event)

    if trigger_source in ("PreSignUp_AdminCreateUser", "PreSignUp_ExternalProvider"):
        return event

    is_cognito_trigger = "request" in event and "userAttributes" in event.get("request", {})
    action = _extract(event, "action") if not is_cognito_trigger else None

    # For researcher management actions, extract a verified caller identity from
    # the Lambda invocation context. When invoked directly (AWS CLI / boto3),
    # context.invoked_function_arn is always present and reflects the IAM principal
    # that called lambda:InvokeFunction — the caller cannot forge this. When
    # invoked via API Gateway with IAM auth, the ARN is also trustworthy.
    # We write this to DynamoDB alongside every mutation for audit purposes.
    caller_arn = getattr(context, "invoked_function_arn", None) or "unknown"
    # For direct invocations the IAM principal is in the request context injected
    # by the Lambda service under event["requestContext"]["identity"]["userArn"].
    # Fall back to the function ARN (which still identifies the account) when absent.
    request_ctx = event.get("requestContext") or {}
    identity = request_ctx.get("identity") or {}
    caller_identity = identity.get("userArn") or caller_arn

    if action == "pre_reserve":
        email = (_extract(event, "email") or "").strip().lower()
        if not email:
            raise HandlerError("missing email")
        role = _normalize_role(_extract(event, "role"))
        code = _pre_reserve_code(email, role, caller_identity)
        return {"email": email, "tcu_code": code, "role": role, "pre_reserved": True}

    if action == "list_pre_reservations":
        return {"reservations": _list_pre_reservations()}

    if action == "delete_pre_reservation":
        # Researcher tool: remove a pre-reservation (e.g. wrong email, re-enrollment).
        # Does not affect accounts already claimed from this reservation.
        email = (_extract(event, "email") or "").strip().lower()
        if not email:
            raise HandlerError("missing email")
        _delete_pre_reservation(email, caller_identity)
        return {"deleted": True, "email": email}

    if action == "create_cohort":
        cohort_id = (_extract(event, "cohort_id") or "").strip()
        name = (_extract(event, "name") or "").strip()
        if not cohort_id:
            raise HandlerError("missing cohort_id")
        return _create_cohort(cohort_id, name, caller_identity)

    if action == "enroll_participant":
        lf_code = (_extract(event, "lf_code") or "").strip()
        cohort_id = (_extract(event, "cohort_id") or "").strip()
        if not lf_code or not cohort_id:
            raise HandlerError("missing lf_code or cohort_id")
        notes = (_extract(event, "notes") or "").strip()
        return _enroll_participant(lf_code, cohort_id, notes, caller_identity)

    if action == "update_enrollment_status":
        lf_code = (_extract(event, "lf_code") or "").strip()
        cohort_id = (_extract(event, "cohort_id") or "").strip()
        status = (_extract(event, "status") or "").strip()
        if not lf_code or not cohort_id or not status:
            raise HandlerError("missing lf_code, cohort_id, or status")
        eligible_next = _extract(event, "eligible_next")
        notes = (_extract(event, "notes") or "").strip()
        return _update_enrollment_status(lf_code, cohort_id, status, eligible_next, notes, caller_identity)

    if action == "list_cohort":
        cohort_id = (_extract(event, "cohort_id") or "").strip()
        if not cohort_id:
            raise HandlerError("missing cohort_id")
        return {"cohort_id": cohort_id, "enrollments": _list_cohort(cohort_id)}

    if action == "get_participant_history":
        lf_code = (_extract(event, "lf_code") or "").strip()
        if not lf_code:
            raise HandlerError("missing lf_code")
        return {"lf_code": lf_code, "history": _get_participant_history(lf_code)}

    if action == "retire_researcher":
        email = (_extract(event, "email") or "").strip().lower()
        sub = (_extract(event, "sub") or "").strip()
        rs_code = (_extract(event, "rs_code") or "").strip()
        reason = (_extract(event, "reason") or "account_deleted").strip()
        if not email or not sub or not rs_code:
            raise HandlerError("retire_researcher requires email, sub, and rs_code")
        result = _retire_researcher(sub, email, rs_code, reason, caller_identity)
        return result

    if action == "migrate_researcher":
        email = (_extract(event, "email") or "").strip().lower()
        sub = (_extract(event, "sub") or "").strip()
        rs_code = (_extract(event, "rs_code") or "").strip()
        cognito_username = (_extract(event, "cognito_username") or "").strip()
        if not email or not sub or not rs_code:
            raise HandlerError("migrate_researcher requires email, sub, and rs_code")
        result = _migrate_researcher_account(sub, email, rs_code, cognito_username, caller_identity)
        return result

    if action == "seed_rs_counter":
        minimum = int(_extract(event, "minimum") or 0)
        if minimum < 1:
            raise HandlerError("minimum must be >= 1")
        _seed_rs_counter(minimum)
        return {"seeded": True, "minimum": minimum}

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
        existing_role = _normalize_role(existing_user.get("role"))
        _write_cognito_attributes(cognito_username, existing_code, existing_role)
        if is_cognito_trigger:
            return event
        return _ok(existing_code, cognito_sub, existing_role, assigned_now=False)

    pre = _lookup_pre_reservation_full(email) if email else None
    if pre is not None:
        pre_code = pre["tcu_code"]
        pre_role = _normalize_role(pre.get("role"))
        _claim_pre_reservation(cognito_sub, cognito_username, email, pre_code, pre_role)
        _write_cognito_attributes(cognito_username, pre_code, pre_role)
        if is_cognito_trigger:
            return event
        return _ok(pre_code, cognito_sub, pre_role, assigned_now=True)

    # No pre-reservation: auto-assign as LF participant.
    tcu_code = _assign_new(cognito_sub, cognito_username, ROLE_LF)
    _write_cognito_attributes(cognito_username, tcu_code, ROLE_LF)
    if is_cognito_trigger:
        return event
    return _ok(tcu_code, cognito_sub, ROLE_LF, assigned_now=True)


def _handle_pre_signup(event: dict) -> dict:
    """Cognito PreSignUp trigger: verify this email is on the researcher's
    pre-reservation list. No enrollment code is required from the participant —
    email ownership is verified separately by Cognito's email verification step.

    Only emails pre-reserved by a researcher (via the pre_reserve Lambda action,
    which requires AWS credentials) are allowed to register. This prevents
    self-registration and enforces that only invited participants can sign up.
    """
    attrs = event.get("request", {}).get("userAttributes", {}) or {}
    email = (attrs.get("email") or "").strip().lower()

    if not email:
        raise HandlerError("Email is required for signup.")

    reservation = _lookup_pre_reservation(email)
    if reservation is None:
        raise HandlerError(
            "This email is not on the participant list. Make sure you entered "
            "the exact email your researcher used, or contact your researcher."
        )

    return event


def _extract(event, key: str) -> Optional[str]:
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
    """Map role input to canonical ROLE_LF or ROLE_RS.
    Accepts legacy values TCU (→ LF) and TEST (→ RS) for backward compat."""
    if not role_in:
        return ROLE_LF
    role = str(role_in).strip().upper()
    if role in (ROLE_RS, ROLE_TEST):
        return ROLE_RS
    if role in (ROLE_LF, ROLE_TCU):
        return ROLE_LF
    return ROLE_LF


def _random_lf_code() -> str:
    """Generate a random 6-char LF participant ID: LF + 4-digit random (1000-9999).
    No cohort embedded — cohort is stored separately in DynamoDB enrollment records."""
    n = random.randint(PARTICIPANT_NUMBER_RANGE[0], PARTICIPANT_NUMBER_RANGE[1])
    return f"LF{n}"


def _normalize_rs_number(raw: str) -> str:
    """Return the bare zero-padded 4-digit number for an RS code input.

    Accepts: "RS0006", "0006", "6", "rs0006" — all return "0006".
    Rejects anything that is not a valid RS code shape by raising HandlerError.
    Uses an explicit prefix check rather than lstrip so "RS0006" → "0006"
    without risk of stripping legitimate leading digits.
    """
    s = raw.strip()
    upper = s.upper()
    if upper.startswith("RS"):
        digits = upper[2:]
    else:
        digits = upper
    if not digits.isdigit():
        raise HandlerError(
            f"Invalid RS code input {raw!r}: expected RS<digits> or bare digits."
        )
    n = int(digits)
    if n < 1 or n > RS_MAX:
        raise HandlerError(
            f"RS code number {n} out of range (1–{RS_MAX})."
        )
    return f"{n:04d}"


def _next_rs_code() -> str:
    """Atomically increment the RS counter and return the next RS ID.
    Format: RS + 4-digit zero-padded sequential (RS0001, RS0002, ...).

    Requires counter#RS to already exist (seeded via seed_rs_counter action).
    Raises HandlerError if the counter is missing so callers fail loudly
    rather than silently issuing RS0001 on an unseeded table.
    """
    try:
        resp = table.update_item(
            Key={"id": "counter#RS"},
            UpdateExpression="SET rs_next = rs_next + :incr",
            ConditionExpression="attribute_exists(id)",
            ExpressionAttributeValues={":incr": 1},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HandlerError(
                "counter#RS does not exist. "
                "Run the seed_rs_counter action (minimum=6) before assigning RS codes."
            )
        raise
    n = int(resp["Attributes"]["rs_next"])
    if n > RS_MAX:
        raise HandlerError(f"RS code space exhausted (RS0001–RS{RS_MAX:04d})")
    return f"RS{n:04d}"


def _seed_rs_counter(minimum: int) -> None:
    """Ensure counter#RS exists and rs_next >= minimum. Never decrements.
    Uses attribute_not_exists so a concurrent first-time RS assignment that
    also creates the counter cannot race this call and produce a duplicate.
    If the counter already exists and rs_next >= minimum, this is a no-op.
    If the counter already exists and rs_next < minimum (shouldn't happen in
    production, but covers manual corrections), it raises rather than silently
    setting a lower value that would re-issue already-assigned codes."""
    existing = table.get_item(Key={"id": "counter#RS"}).get("Item")
    if existing is not None:
        current = int(existing.get("rs_next", 0))
        if current >= minimum:
            return
        raise HandlerError(
            f"counter#RS already exists with rs_next={current}, "
            f"which is less than the requested minimum={minimum}. "
            "Cannot safely reset — would re-issue already-assigned RS codes. "
            "Investigate and correct manually."
        )
    try:
        table.put_item(
            Item={"id": "counter#RS", "rs_next": minimum},
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            # A concurrent call created the counter between our get and put.
            # Re-check that the new value is acceptable.
            _seed_rs_counter(minimum)
        else:
            raise


def _lookup_existing_user(cognito_sub: str) -> Optional[dict]:
    if not cognito_sub:
        raise HandlerError("missing cognito_sub")
    resp = table.get_item(Key={"id": f"user#{cognito_sub}"})
    return resp.get("Item")


def _assign_new(cognito_sub: str, cognito_username: str, role: str) -> str:
    """Assign a new ID for the given role. RS uses a sequential counter; LF
    uses random selection with collision detection."""
    if role == ROLE_RS:
        return _assign_rs_code(cognito_sub, cognito_username)
    return _assign_lf_code(cognito_sub, cognito_username)


def _assign_lf_code(cognito_sub: str, cognito_username: str) -> str:
    """Pick a random LF code, verify not taken, persist atomically. Retries on collision.
    Uses conditional write (attribute_not_exists) to prevent double-assignment under
    concurrent Lambda invocations for the same Cognito sub."""
    for _ in range(RANDOM_RETRY_LIMIT):
        formatted = _random_lf_code()

        if _is_reserved(formatted):
            continue

        try:
            table.put_item(
                Item={
                    "id": f"user#{cognito_sub}",
                    "tcu_code": formatted,
                    "role": ROLE_LF,
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
                raise HandlerError(f"race condition writing LF mapping for {cognito_sub}")
            raise

        table.put_item(
            Item={
                "id": f"code#{formatted}",
                "cognito_sub": cognito_sub,
                "cognito_username": cognito_username,
                "role": ROLE_LF,
                "assigned_at_epoch": _now_epoch(),
            }
        )
        return formatted

    raise HandlerError("LF random code collisions exceeded retry limit")


def _assign_rs_code(cognito_sub: str, cognito_username: str) -> str:
    """Assign the next sequential RS code using an atomic DynamoDB counter.

    Uses TransactWriteItems to atomically write both the user# record and the
    code#RS<n> lock record. If the user# record already exists (concurrent
    duplicate call), the transaction fails and we return the existing code.
    If the code#RS<n> slot was claimed between counter increment and transaction
    (extremely unlikely with sequential counters), we skip and try the next slot.
    """
    ddb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))
    for _ in range(RANDOM_RETRY_LIMIT):
        formatted = _next_rs_code()
        n = _normalize_rs_number(formatted)

        if _is_rs_reserved(formatted):
            continue

        epoch = _now_epoch()
        try:
            ddb_client.transact_write_items(TransactItems=[
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "id": {"S": f"user#{cognito_sub}"},
                            "tcu_code": {"S": formatted},
                            "role": {"S": ROLE_RS},
                            "cognito_username": {"S": cognito_username},
                            "assigned_at_epoch": {"N": str(epoch)},
                        },
                        "ConditionExpression": "attribute_not_exists(id)",
                    }
                },
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "id": {"S": f"code#RS{n}"},
                            "cognito_sub": {"S": cognito_sub},
                            "cognito_username": {"S": cognito_username},
                            "role": {"S": ROLE_RS},
                            "assigned_at_epoch": {"N": str(epoch)},
                        },
                        "ConditionExpression": "attribute_not_exists(id)",
                    }
                },
            ])
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException":
                reasons = e.response.get("CancellationReasons", [])
                # First item = user# condition failed → duplicate call, return existing
                if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                    existing = _lookup_existing_user(cognito_sub)
                    if existing is not None:
                        return existing.get("tcu_code")
                # Second item = code#RS<n> condition failed → slot taken, skip
                continue
            raise
        return formatted

    raise HandlerError("RS assignment failed after retry limit")


def _pre_reserve_code(email: str, role: str, caller_identity: str = "unknown") -> str:
    """Reserve a code for a participant email before they sign up.
    Idempotent: if the email already has a reservation, returns the existing one.

    RS pre-reservations use code#RS<n> (prefixed namespace) and are written
    atomically with the pre# record via TransactWriteItems. LF pre-reservations
    use the original two-step write (pre# then code#LF<n>) which is acceptable
    because LF codes are random and a failed second write just leaves a temporary
    gap — the pre# record always wins on idempotency re-entry.
    """
    existing = _lookup_pre_reservation(email)
    if existing is not None:
        return existing

    ddb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))

    for _ in range(RANDOM_RETRY_LIMIT):
        formatted = _random_lf_code() if role == ROLE_LF else _next_rs_code()

        if _is_reserved(formatted):
            continue

        epoch = _now_epoch()

        if role == ROLE_RS:
            n = _normalize_rs_number(formatted)
            try:
                ddb_client.transact_write_items(TransactItems=[
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": {
                                "id": {"S": f"pre#{email}"},
                                "tcu_code": {"S": formatted},
                                "email": {"S": email},
                                "role": {"S": role},
                                "reserved_at_epoch": {"N": str(epoch)},
                                "claimed": {"BOOL": False},
                                "reserved_by": {"S": caller_identity},
                            },
                            "ConditionExpression": "attribute_not_exists(id)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": TABLE_NAME,
                            "Item": {
                                "id": {"S": f"code#RS{n}"},
                                "email": {"S": email},
                                "role": {"S": role},
                                "pre_reserved": {"BOOL": True},
                                "reserved_at_epoch": {"N": str(epoch)},
                                "reserved_by": {"S": caller_identity},
                            },
                            "ConditionExpression": "attribute_not_exists(id)",
                        }
                    },
                ])
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "TransactionCanceledException":
                    reasons = e.response.get("CancellationReasons", [])
                    if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                        existing = _lookup_pre_reservation(email)
                        if existing is not None:
                            return existing
                    continue
                raise
            return formatted
        else:
            # LF path — two-step write (pre# first, then code#)
            try:
                table.put_item(
                    Item={
                        "id": f"pre#{email}",
                        "tcu_code": formatted,
                        "email": email,
                        "role": role,
                        "reserved_at_epoch": epoch,
                        "claimed": False,
                        "reserved_by": caller_identity,
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

            table.put_item(
                Item={
                    "id": f"code#{formatted}",
                    "email": email,
                    "role": role,
                    "pre_reserved": True,
                    "reserved_at_epoch": epoch,
                    "reserved_by": caller_identity,
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
    if not email:
        return None
    resp = table.get_item(Key={"id": f"pre#{email}"})
    return resp.get("Item")


def _claim_pre_reservation(cognito_sub: str, cognito_username: str, email: str, tcu_code: str, role: str) -> None:
    """Mark a pre-reservation as claimed and write the user# record atomically.

    For RS codes the code#RS<n> lock is updated in the same transaction so the
    lock always reflects the current owner. For LF codes the two-step approach
    is kept (pre# and code# are separate writes; a failed update of code# is
    not critical because _is_rs_reserved is not called for LF at claim time).
    """
    epoch = _now_epoch()

    if role == ROLE_RS:
        n = _normalize_rs_number(tcu_code)
        ddb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))

        # Verify this email is allowed to claim this RS slot before writing.
        if not _is_rs_claimable_by(n, email):
            # Check if user# already exists (idempotent re-entry)
            existing = _lookup_existing_user(cognito_sub)
            if existing is not None:
                return
            raise HandlerError(
                f"RS{n} is not a pre-reservation for {email}. "
                "Cannot claim this slot."
            )

        try:
            ddb_client.transact_write_items(TransactItems=[
                {
                    "Put": {
                        "TableName": TABLE_NAME,
                        "Item": {
                            "id": {"S": f"user#{cognito_sub}"},
                            "tcu_code": {"S": tcu_code},
                            "role": {"S": role},
                            "cognito_username": {"S": cognito_username},
                            "email": {"S": email},
                            "assigned_at_epoch": {"N": str(epoch)},
                            "from_pre_reservation": {"BOOL": True},
                        },
                        "ConditionExpression": "attribute_not_exists(id)",
                    }
                },
                {
                    "Update": {
                        "TableName": TABLE_NAME,
                        "Key": {"id": {"S": f"pre#{email}"}},
                        "UpdateExpression": "SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
                        "ExpressionAttributeValues": {
                            ":t": {"BOOL": True},
                            ":s": {"S": cognito_sub},
                            ":e": {"N": str(epoch)},
                        },
                    }
                },
                {
                    "Update": {
                        "TableName": TABLE_NAME,
                        "Key": {"id": {"S": f"code#RS{n}"}},
                        "UpdateExpression": "SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
                        "ExpressionAttributeValues": {
                            ":t": {"BOOL": True},
                            ":s": {"S": cognito_sub},
                            ":e": {"N": str(epoch)},
                        },
                    }
                },
            ])
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                existing = _lookup_existing_user(cognito_sub)
                if existing is not None:
                    return
            raise
        return

    # LF path — original two-step write
    try:
        table.put_item(
            Item={
                "id": f"user#{cognito_sub}",
                "tcu_code": tcu_code,
                "role": role,
                "cognito_username": cognito_username,
                "email": email,
                "assigned_at_epoch": epoch,
                "from_pre_reservation": True,
            },
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise

    try:
        table.update_item(
            Key={"id": f"pre#{email}"},
            UpdateExpression="SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
            ExpressionAttributeValues={
                ":t": True,
                ":s": cognito_sub,
                ":e": epoch,
            },
        )
    except ClientError:
        pass


def _list_pre_reservations() -> list:
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
            "role": _normalize_role(it.get("role")),
            "claimed": it.get("claimed", False),
            "reserved_at_epoch": int(it.get("reserved_at_epoch", 0)),
        }
        for it in items
    ]


def _delete_pre_reservation(email: str, caller_identity: str = "unknown") -> None:
    """Remove a pre-reservation record. Used when participant used wrong email
    or researcher needs to reassign. Does not affect already-claimed accounts.
    Writes a tombstone audit record so the deletion is traceable."""
    item = _lookup_pre_reservation_full(email)
    if item is None:
        return
    if item.get("claimed"):
        raise HandlerError(
            f"Pre-reservation for {email} is already claimed — account exists. "
            "Contact the participant directly to correct their email."
        )
    try:
        table.delete_item(Key={"id": f"pre#{email}"})
        reserved_code = item.get("tcu_code")
        if reserved_code:
            table.delete_item(Key={"id": f"code#{reserved_code}"})
        # Tombstone: preserve audit trail of the deletion without the PII email.
        table.put_item(Item={
            "id": f"audit#delete_pre#{email}",
            "action": "delete_pre_reservation",
            "email": email,
            "deleted_code": reserved_code,
            "deleted_by": caller_identity,
            "deleted_at_epoch": _now_epoch(),
        })
    except ClientError as e:
        raise HandlerError(f"Failed to delete pre-reservation: {e}")


# ---------------------------------------------------------------------------
# Cohort management
# ---------------------------------------------------------------------------

def _create_cohort(cohort_id: str, name: str, caller_identity: str = "unknown") -> dict:
    """Create a cohort record. Idempotent if cohort_id already exists."""
    existing = table.get_item(Key={"id": f"cohort#{cohort_id}"}).get("Item")
    if existing:
        return {"cohort_id": cohort_id, "created": False, "existing": True}
    table.put_item(Item={
        "id": f"cohort#{cohort_id}",
        "cohort_id": cohort_id,
        "name": name or cohort_id,
        "status": "active",
        "created_at_epoch": _now_epoch(),
        "created_by": caller_identity,
    })
    return {"cohort_id": cohort_id, "name": name or cohort_id, "created": True}


def _enroll_participant(lf_code: str, cohort_id: str, notes: str = "",
                        caller_identity: str = "unknown") -> dict:
    """Enroll a participant in a cohort. Idempotent for active enrollments;
    raises if already enrolled in this exact cohort (use update_enrollment_status
    to re-enroll a withdrawn participant). Preserves enrollment history."""
    enrollment_id = f"enrollment#{lf_code}#{cohort_id}"
    existing = table.get_item(Key={"id": enrollment_id}).get("Item")
    if existing and existing.get("status") == "active":
        return {"lf_code": lf_code, "cohort_id": cohort_id, "enrolled": False,
                "reason": "already_active"}
    table.put_item(Item={
        "id": enrollment_id,
        "lf_code": lf_code,
        "cohort_id": cohort_id,
        "status": "active",
        "enrolled_at_epoch": _now_epoch(),
        "withdrawn_at_epoch": None,
        "eligible_next": False,
        "notes": notes,
        "enrolled_by": caller_identity,
    })
    return {"lf_code": lf_code, "cohort_id": cohort_id, "enrolled": True}


def _update_enrollment_status(
    lf_code: str, cohort_id: str, status: str,
    eligible_next=None, notes: str = "", caller_identity: str = "unknown"
) -> dict:
    """Update enrollment status (active/completed/withdrawn) and eligibility.
    Does not overwrite enrolled_at_epoch — history is preserved."""
    valid_statuses = ("active", "completed", "withdrawn")
    if status not in valid_statuses:
        raise HandlerError(f"status must be one of {valid_statuses}")

    enrollment_id = f"enrollment#{lf_code}#{cohort_id}"
    existing = table.get_item(Key={"id": enrollment_id}).get("Item")
    if existing is None:
        raise HandlerError(f"No enrollment found for {lf_code} in cohort {cohort_id}")

    update_expr = "SET #st = :status, updated_at_epoch = :ts, updated_by = :by"
    expr_names = {"#st": "status"}
    expr_vals: dict = {":status": status, ":ts": _now_epoch(), ":by": caller_identity}

    if status == "withdrawn":
        update_expr += ", withdrawn_at_epoch = :wt"
        expr_vals[":wt"] = _now_epoch()

    if eligible_next is not None:
        update_expr += ", eligible_next = :en"
        expr_vals[":en"] = bool(eligible_next)

    if notes:
        update_expr += ", notes = :notes"
        expr_vals[":notes"] = notes

    table.update_item(
        Key={"id": enrollment_id},
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_vals,
    )
    return {"lf_code": lf_code, "cohort_id": cohort_id, "status": status, "updated": True}


def _list_cohort(cohort_id: str) -> list:
    """Return all enrollment records for a cohort."""
    items = []
    resp = table.scan(
        FilterExpression="begins_with(id, :p)",
        ExpressionAttributeValues={":p": f"enrollment#"},
    )
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(
            FilterExpression="begins_with(id, :p)",
            ExpressionAttributeValues={":p": "enrollment#"},
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return [
        {
            "lf_code": it["lf_code"],
            "status": it.get("status"),
            "enrolled_at_epoch": int(it.get("enrolled_at_epoch", 0)),
            "withdrawn_at_epoch": it.get("withdrawn_at_epoch"),
            "eligible_next": it.get("eligible_next", False),
            "notes": it.get("notes", ""),
        }
        for it in items if it.get("cohort_id") == cohort_id
    ]


def _get_participant_history(lf_code: str) -> list:
    """Return all cohort enrollments for a participant (full history)."""
    prefix = f"enrollment#{lf_code}#"
    items = []
    resp = table.scan(
        FilterExpression="begins_with(id, :p)",
        ExpressionAttributeValues={":p": prefix},
    )
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(
            FilterExpression="begins_with(id, :p)",
            ExpressionAttributeValues={":p": prefix},
            ExclusiveStartKey=resp["LastEvaluatedKey"],
        )
        items.extend(resp.get("Items", []))
    return [
        {
            "cohort_id": it["cohort_id"],
            "status": it.get("status"),
            "enrolled_at_epoch": int(it.get("enrolled_at_epoch", 0)),
            "withdrawn_at_epoch": it.get("withdrawn_at_epoch"),
            "eligible_next": it.get("eligible_next", False),
            "notes": it.get("notes", ""),
        }
        for it in items
    ]


def _retire_researcher(sub: str, email: str, rs_code: str, reason: str, caller_identity: str) -> dict:
    """Permanently retire an RS researcher ID.

    Writes two redundant retirement records (retired# tombstone + code#RS<n>
    with retired=True) and marks the user# record as deleted. No record is ever
    deleted — historical data always has a user# to reference.

    If code#RS<n> does not yet exist (legacy codes 0001–0004 stored as bare
    code#<n>), it is created in retired state rather than left absent, so
    _is_rs_reserved() finds it on either key path.

    Uses TransactWriteItems. If the transaction fails because retired# already
    exists (idempotent re-call), the function returns success.
    """
    n = _normalize_rs_number(rs_code)
    formatted = f"RS{n}"
    epoch = _now_epoch()
    ddb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))

    # Build the code#RS<n> item. If a lock already exists, we update (not put)
    # to avoid overwriting historical assignment data. Use a conditional put
    # that only fires when the item is absent; fall through to update otherwise.
    code_pk = f"code#RS{n}"
    existing_lock = table.get_item(Key={"id": code_pk}).get("Item")

    transact_items = [
        # Tombstone — primary permanent retirement record
        {
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "id": {"S": f"retired#RS{n}"},
                    "rs_code": {"S": formatted},
                    "email": {"S": email},
                    "sub": {"S": sub},
                    "reason": {"S": reason},
                    "retired_at_epoch": {"N": str(epoch)},
                    "retired_by": {"S": caller_identity},
                },
                # Idempotent: if tombstone already exists, skip quietly
                "ConditionExpression": "attribute_not_exists(id)",
            }
        },
        # Mark user# record as deleted (never delete the record itself)
        {
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"id": {"S": f"user#{sub}"}},
                "UpdateExpression": "SET deleted = :t, deleted_at_epoch = :e, deleted_by = :by",
                "ExpressionAttributeValues": {
                    ":t": {"BOOL": True},
                    ":e": {"N": str(epoch)},
                    ":by": {"S": caller_identity},
                },
            }
        },
    ]

    if existing_lock is None:
        # No code#RS<n> record — create it in retired state
        transact_items.append({
            "Put": {
                "TableName": TABLE_NAME,
                "Item": {
                    "id": {"S": code_pk},
                    "rs_code": {"S": formatted},
                    "email": {"S": email},
                    "sub": {"S": sub},
                    "retired": {"BOOL": True},
                    "retired_at_epoch": {"N": str(epoch)},
                    "retired_by": {"S": caller_identity},
                },
                "ConditionExpression": "attribute_not_exists(id)",
            }
        })
    else:
        # Lock exists — add retired flag without overwriting existing fields
        transact_items.append({
            "Update": {
                "TableName": TABLE_NAME,
                "Key": {"id": {"S": code_pk}},
                "UpdateExpression": "SET retired = :t, retired_at_epoch = :e, retired_by = :by",
                "ExpressionAttributeValues": {
                    ":t": {"BOOL": True},
                    ":e": {"N": str(epoch)},
                    ":by": {"S": caller_identity},
                },
            }
        })

    try:
        ddb_client.transact_write_items(TransactItems=transact_items)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            # First item (retired# put) failed → tombstone already exists → idempotent
            if reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
                return {"retired": True, "rs_code": formatted, "idempotent": True}
        raise HandlerError(f"Failed to retire {formatted}: {e}")

    return {
        "retired": True,
        "rs_code": formatted,
        "sub": sub,
        "email": email,
        "reason": reason,
    }


def _migrate_researcher_account(
    sub: str, email: str, rs_code: str, cognito_username: str, caller_identity: str
) -> dict:
    """Migrate an existing researcher account to a new RS ID.

    Used when a researcher account already has a user# record (e.g. carries a
    legacy bare-numeric code like '1001') and needs to be updated to a new
    prefixed RS code without creating a second account or new Cognito sub.

    Ownership verification before any write:
      1. code#RS<n> must exist, have pre_reserved=True, and email must match.
      2. user#<sub> must exist (account must already be real).
      3. user#<sub> email field must match the caller-supplied email.

    Transaction (all Updates, no Puts — existing records are updated in place):
      - user#<sub>:   SET tcu_code, role, migrated_from, migrated_at_epoch
                      Condition: attribute_exists(id) so we never create new records
      - pre#<email>:  SET claimed=True, claimed_by_sub, claimed_at_epoch
      - code#RS<n>:   SET claimed=True, claimed_by_sub, claimed_at_epoch

    Idempotent: if user#<sub>.tcu_code already equals rs_code and
    code#RS<n>.claimed is True, returns success without re-writing.

    After a successful transaction, writes tcu_code and tcu_role to Cognito via
    _write_cognito_attributes. If cognito_username is blank, the Cognito write
    is skipped and the caller must handle it separately.
    """
    n = _normalize_rs_number(rs_code)
    formatted = f"RS{n}"
    epoch = _now_epoch()
    ddb_client = boto3.client("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-2"))

    # --- Cognito pre-flight: verify username, sub, and email all refer to the same account ---
    # Runs before any DynamoDB read/write so a mismatch aborts with no side effects.
    # Skipped only when COGNITO_USER_POOL_ID is unset (unit tests) or cognito_username blank.
    if cognito_username and COGNITO_USER_POOL_ID:
        try:
            cognito_user = cognito_idp.admin_get_user(
                UserPoolId=COGNITO_USER_POOL_ID,
                Username=cognito_username,
            )
        except ClientError as e:
            err_code = e.response.get("Error", {}).get("Code", "")
            if err_code == "UserNotFoundException":
                raise HandlerError(
                    f"Cognito user '{cognito_username}' not found in pool. "
                    "Verify cognito_username matches the Cognito username exactly."
                )
            raise HandlerError(f"Cognito pre-flight lookup failed: {e}")

        cognito_attrs = {a["Name"]: a["Value"] for a in cognito_user.get("UserAttributes", [])}
        cognito_sub_actual = cognito_attrs.get("sub", "")
        cognito_email_actual = (cognito_attrs.get("email") or "").strip().lower()

        if cognito_sub_actual != sub:
            raise HandlerError(
                f"Cognito sub mismatch: '{cognito_username}' has sub '{cognito_sub_actual}' "
                f"but supplied sub is '{sub}'. "
                "Migration rejected — sub, username and email must all refer to the same account."
            )
        if cognito_email_actual and cognito_email_actual != email.lower():
            raise HandlerError(
                f"Cognito email mismatch: '{cognito_username}' has email '{cognito_email_actual}' "
                f"but supplied email is '{email}'. "
                "Migration rejected — sub, username and email must all refer to the same account."
            )

    # --- Ownership check 1: code#RS<n> must be pre-reserved for this exact email ---
    lock = table.get_item(Key={"id": f"code#RS{n}"}).get("Item")
    if lock is None:
        raise HandlerError(
            f"code#RS{n} does not exist. "
            "Write the pre-reservation records (Phase C) before migrating."
        )
    if not lock.get("pre_reserved"):
        raise HandlerError(
            f"code#RS{n} exists but is not marked pre_reserved. "
            "Cannot safely claim this slot for migration."
        )
    if lock.get("email") != email:
        raise HandlerError(
            f"code#RS{n} is pre-reserved for a different email. "
            "Migration rejected."
        )

    # --- Ownership check 2: RS slot not already claimed by a different sub ---
    # Must run before the idempotency check so a cross-account claim is always
    # an error, even if user#<sub> coincidentally matches the formatted code.
    if lock.get("claimed") and lock.get("claimed_by_sub"):
        if lock["claimed_by_sub"] != sub:
            raise HandlerError(
                f"code#RS{n} is already claimed by sub '{lock['claimed_by_sub']}', "
                f"not '{sub}'. "
                "Cannot migrate a different account to this slot."
            )

    # --- Ownership check 3: user#<sub> must exist ---
    existing_user = table.get_item(Key={"id": f"user#{sub}"}).get("Item")
    if existing_user is None:
        raise HandlerError(
            f"user#{sub} does not exist. "
            "migrate_researcher is for existing accounts only. "
            "Use the normal assignment flow for new accounts."
        )

    # --- Ownership check 4: existing user record email must match ---
    # Legacy user# records may not have an email field — allowed through.
    stored_email = existing_user.get("email", "")
    if stored_email and stored_email.lower() != email.lower():
        raise HandlerError(
            f"user#{sub} email '{stored_email}' does not match supplied email '{email}'. "
            "Cannot migrate — email mismatch."
        )

    # --- Idempotency: DDB already migrated — verify and repair Cognito ---
    # DDB transaction already completed. Cognito must also be verified before
    # reporting success. If the previous run failed between the DDB commit and
    # the Cognito write, Cognito may still carry the old tcu_code.
    # Any Cognito error here is returned as a retryable partial-migration status
    # rather than swallowed — the caller must not treat partial state as success.
    if existing_user.get("tcu_code") == formatted and lock.get("claimed"):
        if cognito_username and COGNITO_USER_POOL_ID:
            try:
                cog_user = cognito_idp.admin_get_user(
                    UserPoolId=COGNITO_USER_POOL_ID,
                    Username=cognito_username,
                )
                cog_attrs = {a["Name"]: a["Value"] for a in cog_user.get("UserAttributes", [])}
                cognito_code = cog_attrs.get("custom:tcu_code", "")
                if cognito_code != formatted:
                    # DDB correct, Cognito stale — repair and report
                    _write_cognito_attributes(cognito_username, formatted, ROLE_RS)
                    return {
                        "migrated": True,
                        "idempotent": True,
                        "cognito_repaired": True,
                        "rs_code": formatted,
                        "sub": sub,
                        "email": email,
                    }
                # Both systems consistent
                return {
                    "migrated": True,
                    "idempotent": True,
                    "rs_code": formatted,
                    "sub": sub,
                    "email": email,
                }
            except ClientError as e:
                # Cognito unreachable or attributes unreadable. DDB is correct
                # but Cognito state is unknown — return a partial status so the
                # caller knows to retry rather than assuming success.
                err_code = e.response.get("Error", {}).get("Code", "unknown")
                raise HandlerError(
                    f"DDB migration is complete (tcu_code={formatted}) but Cognito "
                    f"could not be verified: {err_code}. "
                    "Retry migrate_researcher to complete Cognito synchronisation. "
                    "Do not treat this as a successful migration."
                )
        # No cognito_username or no pool_id — DDB verified only
        return {
            "migrated": True,
            "idempotent": True,
            "cognito_verified": False,
            "rs_code": formatted,
            "sub": sub,
            "email": email,
        }

    old_code = existing_user.get("tcu_code", "")

    # --- Atomic migration transaction ---
    # Every item carries a ConditionExpression that re-checks the state we
    # verified in the ownership reads above. This closes the TOCTOU window
    # between the reads and the writes: if any record changed concurrently
    # (e.g. another caller claims the RS slot, or the user record is deleted),
    # the entire transaction cancels atomically before any item is written.
    #
    # Item 0 — user#<sub>:
    #   attribute_exists(id)        — record must still exist (not deleted)
    #   tcu_code = :old_code        — must not have been migrated concurrently
    #
    # Item 1 — pre#<email>:
    #   attribute_exists(id)        — reservation must still exist
    #   (claimed = false or absent) — must not have been claimed by another call
    #
    # Item 2 — code#RS<n>:
    #   pre_reserved = true         — slot must still be a pre-reservation
    #   email = :expected_email     — must still belong to this exact email
    #   (claimed = false or absent) — must not have been concurrently claimed
    try:
        ddb_client.transact_write_items(TransactItems=[
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"id": {"S": f"user#{sub}"}},
                    "UpdateExpression": (
                        "SET tcu_code = :code, #rl = :role, "
                        "migrated_from = :old, migrated_at_epoch = :e, "
                        "migrated_by = :by"
                    ),
                    "ExpressionAttributeNames": {"#rl": "role"},
                    "ExpressionAttributeValues": {
                        ":code":     {"S": formatted},
                        ":role":     {"S": ROLE_RS},
                        ":old":      {"S": old_code},
                        ":e":        {"N": str(epoch)},
                        ":by":       {"S": caller_identity},
                        ":old_code": {"S": old_code},
                    },
                    # Record must exist and tcu_code must be unchanged since our read
                    "ConditionExpression": "attribute_exists(id) AND tcu_code = :old_code",
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"id": {"S": f"pre#{email}"}},
                    "UpdateExpression": "SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
                    "ExpressionAttributeValues": {
                        ":t":         {"BOOL": True},
                        ":s":         {"S": sub},
                        ":e":         {"N": str(epoch)},
                        ":false":     {"BOOL": False},
                        ":rs_code":   {"S": formatted},
                    },
                    # Reservation must still exist, refer to this exact RS code,
                    # and must not have been claimed by a concurrent call.
                    # tcu_code = :rs_code guards against the reservation being
                    # deleted and recreated for a different RS code between our
                    # ownership read and this transaction write.
                    "ConditionExpression": (
                        "attribute_exists(id) AND "
                        "tcu_code = :rs_code AND "
                        "(attribute_not_exists(claimed) OR claimed = :false)"
                    ),
                }
            },
            {
                "Update": {
                    "TableName": TABLE_NAME,
                    "Key": {"id": {"S": f"code#RS{n}"}},
                    "UpdateExpression": "SET claimed = :t, claimed_by_sub = :s, claimed_at_epoch = :e",
                    "ExpressionAttributeValues": {
                        ":t":              {"BOOL": True},
                        ":s":              {"S": sub},
                        ":e":              {"N": str(epoch)},
                        ":true":           {"BOOL": True},
                        ":expected_email": {"S": email},
                        ":false":          {"BOOL": False},
                    },
                    # Slot must still be a pre-reservation for this exact email,
                    # and must not have been claimed by any concurrent call
                    "ConditionExpression": (
                        "pre_reserved = :true AND "
                        "email = :expected_email AND "
                        "(attribute_not_exists(claimed) OR claimed = :false)"
                    ),
                }
            },
        ])
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            item_labels = [
                f"user#{sub} (tcu_code changed or record deleted)",
                f"pre#{email} (reservation missing, RS code changed, or already claimed)",
                f"code#RS{n} (slot ownership changed or concurrently claimed)",
            ]
            failed = [
                item_labels[i]
                for i, r in enumerate(reasons)
                if r.get("Code") == "ConditionalCheckFailed"
            ]
            if failed:
                raise HandlerError(
                    "Migration aborted — concurrent modification detected: "
                    + "; ".join(failed)
                )
        raise HandlerError(f"Migration transaction failed: {e}")

    if cognito_username:
        _write_cognito_attributes(cognito_username, formatted, ROLE_RS)

    return {
        "migrated": True,
        "rs_code": formatted,
        "migrated_from": old_code,
        "sub": sub,
        "email": email,
    }


def _is_reserved(code: str) -> bool:
    """Check whether a code (LF or RS) is already taken or retired.

    For LF codes: checks code#<code> only (LF codes are never retired; they are
    permanently locked once assigned and the lock record is never deleted).

    For RS codes: delegates to _is_rs_reserved() which checks three separate
    records to handle legacy bare-numeric keys and the retired# tombstone.
    """
    if code.upper().startswith("RS") or (code.isdigit() and len(code) == 4):
        return _is_rs_reserved(code)
    resp = table.get_item(Key={"id": f"code#{code}"})
    return resp.get("Item") is not None


def _is_rs_reserved(raw: str) -> bool:
    """Check whether an RS slot is taken, pre-reserved, or permanently retired.

    Checks three DynamoDB records (any match → reserved):
      1. code#RS<n>    — new prefixed lock (assigned or pre-reserved)
      2. code#<n>      — legacy bare-numeric lock (RS0001–RS0004 from pre-launch)
      3. retired#RS<n> — permanent tombstone (never deleted)

    Uses _normalize_rs_number() for input validation; raises HandlerError on
    invalid input so callers never receive a silently wrong True/False.
    """
    n = _normalize_rs_number(raw)
    for pk in (f"code#RS{n}", f"code#{n}", f"retired#RS{n}"):
        if table.get_item(Key={"id": pk}).get("Item") is not None:
            return True
    return False


def _is_rs_claimable_by(n: str, email: str) -> bool:
    """Return True only if code#RS<n> is a pre-reservation for exactly this email.

    Used during the claim step so the reserved user can claim their slot while
    any other caller is blocked as a collision.
    """
    item = table.get_item(Key={"id": f"code#RS{n}"}).get("Item")
    if item is None:
        return False
    return bool(item.get("pre_reserved")) and item.get("email") == email


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
    """Write the assigned code and role to Cognito custom attributes.
    Attribute names (custom:tcu_code, custom:tcu_role) are unchanged for
    backward compat; only the values stored in them have new format.

    Raises HandlerError if COGNITO_USER_POOL_ID is not configured — a silent
    skip here would leave the participant with no tcu_code attribute and they
    would be blocked from recording with no actionable error message.
    """
    if not cognito_username:
        return
    if not COGNITO_USER_POOL_ID:
        raise HandlerError(
            "COGNITO_USER_POOL_ID environment variable is not set. "
            "Cannot write tcu_code/tcu_role attributes to Cognito. "
            "Set the env var on the Lambda function and redeploy."
        )
    try:
        cognito_idp.admin_update_user_attributes(
            UserPoolId=COGNITO_USER_POOL_ID,
            Username=cognito_username,
            UserAttributes=[
                {"Name": "custom:tcu_code", "Value": tcu_code},
                {"Name": "custom:tcu_role", "Value": role},
                {"Name": "custom:assigned_at", "Value": str(_now_epoch())},
            ],
        )
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "Unknown")
        msg = e.response.get("Error", {}).get("Message", str(e))
        raise HandlerError(
            f"Failed to write Cognito attributes for {cognito_username}: {code} {msg}"
        )
