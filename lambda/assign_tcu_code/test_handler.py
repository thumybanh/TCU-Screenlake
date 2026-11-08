"""Local unit tests for handler.py.
Run: python3 -m pytest lambda/assign_tcu_code/test_handler.py -v

ID formats under test (both exactly 6 characters):
  LF + 4 digits (1000-9999, no leading zero) — e.g. LF4342
  RS + 4 zero-padded sequential digits       — e.g. RS0001
"""
from __future__ import annotations

import os
os.environ["TCU_TABLE_NAME"] = "test-table"
os.environ["COGNITO_USER_POOL_ID"] = "us-east-1_TESTPOOL"

from unittest.mock import MagicMock, patch
import re
import pytest

import handler as H
from botocore.exceptions import ClientError


LF_PATTERN = re.compile(r"^LF[1-9]\d{3}$")   # LF4342 — 6 chars
RS_PATTERN = re.compile(r"^RS\d{4}$")          # RS0001 — 6 chars
LEGACY_PATTERN = re.compile(r"^\d{4}$")        # 1234  — backward compat


class FakeTable:
    """In-memory DynamoDB-like store for handler.py tests."""

    def __init__(self, prefills: dict | None = None):
        self.items: dict[str, dict] = dict(prefills or {})

    def get_item(self, Key):
        item = self.items.get(Key["id"])
        return {"Item": item} if item is not None else {}

    def put_item(self, Item, ConditionExpression: str | None = None):
        if ConditionExpression == "attribute_not_exists(id)" and Item["id"] in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.items[Item["id"]] = Item
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ReturnValues=None,
                    ConditionExpression=None, **kwargs):
        current = self.items.get(Key["id"], {})
        # Enforce attribute_exists condition (used by _next_rs_code)
        if ConditionExpression and "attribute_exists" in ConditionExpression:
            if Key["id"] not in self.items:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no item"}},
                    "UpdateItem",
                )
        # RS counter increment path
        if ":incr" in ExpressionAttributeValues:
            prior = current.get("rs_next", ExpressionAttributeValues.get(":start", 0))
            new_val = prior + ExpressionAttributeValues[":incr"]
            updated = {**current, "id": Key["id"], "rs_next": new_val}
            self.items[Key["id"]] = updated
            return {"Attributes": {"rs_next": new_val}}
        # Generic SET path: apply :status and other scalar values
        updated = dict(current)
        updated["id"] = Key["id"]
        for k, v in ExpressionAttributeValues.items():
            field = k.lstrip(":")
            # Resolve expression attribute name aliases
            alias = f"#{field}"
            if ExpressionAttributeNames and alias in ExpressionAttributeNames:
                field = ExpressionAttributeNames[alias]
            updated[field] = v
        self.items[Key["id"]] = updated
        return {"Attributes": updated}

    def delete_item(self, Key, **kwargs):
        self.items.pop(Key["id"], None)
        return {}

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None, ExclusiveStartKey=None):
        prefix = (ExpressionAttributeValues or {}).get(":p", "")
        items = [v for k, v in self.items.items() if k.startswith(prefix)]
        return {"Items": items}


class FakeContext:
    """Minimal Lambda context stub with a verifiable invoked_function_arn."""
    def __init__(self, arn="arn:aws:iam::123456789:user/test-researcher"):
        self.invoked_function_arn = arn


class FakeCognitoIdp:
    """Stub for cognito_idp — records calls without hitting AWS."""
    def __init__(self, users: dict | None = None):
        self.calls = []
        # users: {username: {"sub": "...", "email": "..."}}
        self._users = users or {}

    def admin_update_user_attributes(self, **kwargs):
        self.calls.append(kwargs)
        return {}

    def admin_get_user(self, UserPoolId, Username):
        if Username not in self._users:
            raise ClientError(
                {"Error": {"Code": "UserNotFoundException", "Message": "User not found"}},
                "AdminGetUser",
            )
        u = self._users[Username]
        # Return every key in the user dict as a Cognito attribute so tests can
        # inject custom:tcu_code and other attributes without special-casing.
        return {
            "Username": Username,
            "UserAttributes": [
                {"Name": k, "Value": str(v)} for k, v in u.items()
            ],
        }


def _run(event, prefills=None, ctx=None, cognito_users=None):
    """Run lambda_handler with FakeTable and FakeDdbClient patched in.

    FakeDdbClient is constructed lazily after FakeTable so it shares the same
    items dict — defined here after FakeDdbClient is declared below. We use a
    forward-compatible approach: define the helper once and reference the class
    by name after it is declared.
    """
    fake = FakeTable(prefills=prefills)
    fake_cognito = FakeCognitoIdp(users=cognito_users)
    # FakeDdbClient is defined later in the file; at call time it always exists.
    fake_client = _make_fake_client(fake)
    with patch.object(H, "table", fake), \
         patch.object(H, "cognito_idp", fake_cognito), \
         patch("handler.boto3.client", return_value=fake_client):
        result = H.lambda_handler(event, ctx or FakeContext())
    return result, fake


def _make_fake_client(fake_table):
    """Deferred construction so _run() can be defined before FakeDdbClient."""
    return FakeDdbClient(fake_table)  # noqa: F821 — defined later in this file


# ---------------------------------------------------------------------------
# LF participant assignment
# ---------------------------------------------------------------------------

def test_lf_format_is_6_chars():
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    code = result["tcu_code"]
    assert len(code) == 6, f"expected 6 chars, got {len(code)}: {code}"
    assert LF_PATTERN.match(code), f"unexpected LF code format: {code}"


def test_lf_no_cohort_embedded():
    """Code must start with LF followed directly by 4 digits — no cohort digit."""
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    code = result["tcu_code"]
    assert code.startswith("LF")
    assert len(code) == 6
    digit_part = code[2:]
    assert digit_part.isdigit() and len(digit_part) == 4


def test_lf_participant_number_in_range_1000_9999():
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    n = int(result["tcu_code"][2:])
    assert 1000 <= n <= 9999, f"participant number {n} out of range"


def test_lf_assigned_now_true():
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    assert result["assigned_now"] is True
    assert result["role"] == H.ROLE_LF


def test_lf_stored_in_dynamodb():
    result, tbl = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    code = result["tcu_code"]
    assert tbl.items["user#user-A"]["tcu_code"] == code
    assert tbl.items[f"code#{code}"]["cognito_sub"] == "user-A"


def test_two_lf_users_different_codes():
    _, tbl = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    result2, _ = _run({"cognito_sub": "user-B", "cognito_username": "bob"},
                      prefills=tbl.items)
    code_a = tbl.items["user#user-A"]["tcu_code"]
    assert code_a != result2["tcu_code"]


def test_lf_reinvocation_idempotent():
    result1, tbl = _run({"cognito_sub": "user-A", "cognito_username": "alice"})
    result2, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                      prefills=tbl.items)
    assert result1["tcu_code"] == result2["tcu_code"]
    assert result2["assigned_now"] is False


def test_lf_reserved_code_skipped():
    prefills = {"code#LF1000": {"id": "code#LF1000", "cognito_sub": "other"}}
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=prefills)
    assert result["tcu_code"] != "LF1000"
    assert LF_PATTERN.match(result["tcu_code"])


# ---------------------------------------------------------------------------
# RS researcher assignment
# ---------------------------------------------------------------------------

_RS_COUNTER_START = {"counter#RS": {"id": "counter#RS", "rs_next": 0}}


def test_rs_format_is_6_chars():
    result, _ = _run({
        "action": "pre_reserve",
        "email": "researcher@tcu.edu",
        "role": "RS",
    }, prefills=dict(_RS_COUNTER_START))
    code = result["tcu_code"]
    assert len(code) == 6, f"expected 6 chars, got {len(code)}: {code}"
    assert RS_PATTERN.match(code), f"unexpected RS code format: {code}"


def test_rs_first_assignment_is_rs0001():
    result, _ = _run({
        "action": "pre_reserve",
        "email": "researcher@tcu.edu",
        "role": "RS",
    }, prefills=dict(_RS_COUNTER_START))
    assert result["tcu_code"] == "RS0001"


def test_rs_sequential_increments():
    _, tbl1 = _run({
        "action": "pre_reserve",
        "email": "ra1@tcu.edu",
        "role": "RS",
    }, prefills=dict(_RS_COUNTER_START))
    result2, _ = _run({
        "action": "pre_reserve",
        "email": "ra2@tcu.edu",
        "role": "RS",
    }, prefills=tbl1.items)
    assert result2["tcu_code"] == "RS0002"


def test_rs_zero_padded_to_4_digits():
    """RS0042 not RS42 — always 4 zero-padded digits."""
    prefills = {"counter#RS": {"id": "counter#RS", "rs_next": 41}}
    result, _ = _run({
        "action": "pre_reserve",
        "email": "researcher@tcu.edu",
        "role": "RS",
    }, prefills=prefills)
    assert result["tcu_code"] == "RS0042"


# ---------------------------------------------------------------------------
# Role normalization — backward compat with legacy TCU/TEST values
# ---------------------------------------------------------------------------

def test_normalize_legacy_tcu_to_lf():
    assert H._normalize_role("TCU") == H.ROLE_LF


def test_normalize_legacy_test_to_rs():
    assert H._normalize_role("TEST") == H.ROLE_RS


def test_normalize_lf():
    assert H._normalize_role("LF") == H.ROLE_LF


def test_normalize_rs():
    assert H._normalize_role("RS") == H.ROLE_RS


def test_normalize_none_defaults_to_lf():
    assert H._normalize_role(None) == H.ROLE_LF


def test_existing_tcu_account_returned_unchanged():
    """Legacy 4-digit code preserved; role normalized TCU→LF."""
    prefills = {
        "user#user-A": {"id": "user#user-A", "tcu_code": "1234", "role": "TCU"},
    }
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=prefills)
    assert result["tcu_code"] == "1234"
    assert result["role"] == H.ROLE_LF
    assert result["assigned_now"] is False


def test_existing_test_account_returned_unchanged():
    """Legacy TEST researcher code preserved; role normalized TEST→RS."""
    prefills = {
        "user#user-B": {"id": "user#user-B", "tcu_code": "0042", "role": "TEST"},
    }
    result, _ = _run({"cognito_sub": "user-B", "cognito_username": "bob"},
                     prefills=prefills)
    assert result["tcu_code"] == "0042"
    assert result["role"] == H.ROLE_RS
    assert result["assigned_now"] is False


# ---------------------------------------------------------------------------
# Pre-reservation
# ---------------------------------------------------------------------------

def test_pre_reserve_lf():
    result, tbl = _run({
        "action": "pre_reserve",
        "email": "participant@example.com",
        "role": "LF",
    })
    code = result["tcu_code"]
    assert LF_PATTERN.match(code), f"bad LF pre-reservation code: {code}"
    assert result["pre_reserved"] is True
    assert tbl.items["pre#participant@example.com"]["tcu_code"] == code


def test_pre_reserve_idempotent():
    result1, tbl = _run({"action": "pre_reserve", "email": "p@example.com", "role": "LF"})
    result2, _ = _run({"action": "pre_reserve", "email": "p@example.com", "role": "LF"},
                      prefills=tbl.items)
    assert result1["tcu_code"] == result2["tcu_code"]


# ---------------------------------------------------------------------------
# API Gateway body-wrapped payload
# ---------------------------------------------------------------------------

def test_api_gateway_body_wrapped():
    import json as _json
    event = {"body": _json.dumps({"cognito_sub": "user-A", "cognito_username": "alice"})}
    result, _ = _run(event)
    assert LF_PATTERN.match(result["tcu_code"])


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_missing_cognito_sub_raises():
    with pytest.raises(H.HandlerError, match="missing cognito_sub"):
        _run({"cognito_username": "alice"})


def test_race_condition_returns_existing():
    prefills = {
        "user#user-A": {"id": "user#user-A", "tcu_code": "LF4342", "role": "LF"},
    }
    result, _ = _run({"cognito_sub": "user-A", "cognito_username": "alice"},
                     prefills=prefills)
    assert result["tcu_code"] == "LF4342"
    assert result["assigned_now"] is False


# ---------------------------------------------------------------------------
# Phase 2 — email-only PreSignUp (no enrollment code required)
# ---------------------------------------------------------------------------

def _pre_signup_event(email: str) -> dict:
    return {
        "triggerSource": "PreSignUp_SignUp",
        "request": {"userAttributes": {"email": email, "sub": "sub-123"}},
        "response": {},
    }


def test_pre_signup_allowed_email_passes():
    prefills = {
        "pre#alice@example.com": {
            "id": "pre#alice@example.com",
            "tcu_code": "LF4342",
            "role": "LF",
        }
    }
    result, _ = _run(_pre_signup_event("alice@example.com"), prefills=prefills)
    # Trigger must return the event unchanged on success
    assert result["triggerSource"] == "PreSignUp_SignUp"


def test_pre_signup_unknown_email_raises():
    with pytest.raises(H.HandlerError, match="not on the participant list"):
        _run(_pre_signup_event("stranger@example.com"))


def test_pre_signup_missing_email_raises():
    event = {
        "triggerSource": "PreSignUp_SignUp",
        "request": {"userAttributes": {}},
        "response": {},
    }
    with pytest.raises(H.HandlerError, match="Email is required"):
        _run(event)


def test_pre_signup_admin_create_bypasses_check():
    """AdminCreateUser trigger must pass through without checking reservations."""
    event = {
        "triggerSource": "PreSignUp_AdminCreateUser",
        "request": {"userAttributes": {"email": "nobody@example.com"}},
        "response": {},
    }
    result, _ = _run(event)
    assert result["triggerSource"] == "PreSignUp_AdminCreateUser"


# ---------------------------------------------------------------------------
# Phase 4 — cohort management
# ---------------------------------------------------------------------------

def test_create_cohort():
    result, tbl = _run({"action": "create_cohort", "cohort_id": "2026-spring", "name": "Spring 2026"})
    assert result["cohort_id"] == "2026-spring"
    assert "cohort#2026-spring" in tbl.items


def test_create_cohort_missing_id_raises():
    with pytest.raises(H.HandlerError, match="missing cohort_id"):
        _run({"action": "create_cohort", "name": "No ID"})


def test_enroll_participant():
    prefills = {"cohort#c1": {"id": "cohort#c1", "name": "C1"}}
    result, tbl = _run(
        {"action": "enroll_participant", "lf_code": "LF4342", "cohort_id": "c1"},
        prefills=prefills,
    )
    assert result["lf_code"] == "LF4342"
    assert result["cohort_id"] == "c1"
    assert "enrollment#LF4342#c1" in tbl.items


def test_enroll_participant_missing_fields_raises():
    with pytest.raises(H.HandlerError, match="missing lf_code or cohort_id"):
        _run({"action": "enroll_participant", "lf_code": "LF4342"})


def test_update_enrollment_status():
    prefills = {
        "enrollment#LF4342#c1": {
            "id": "enrollment#LF4342#c1",
            "lf_code": "LF4342",
            "cohort_id": "c1",
            "status": "active",
        }
    }
    result, tbl = _run(
        {"action": "update_enrollment_status", "lf_code": "LF4342", "cohort_id": "c1",
         "status": "completed"},
        prefills=prefills,
    )
    assert result["status"] == "completed"


def test_list_cohort():
    prefills = {
        "enrollment#LF4342#c1": {"id": "enrollment#LF4342#c1", "lf_code": "LF4342", "cohort_id": "c1"},
        "enrollment#LF5555#c1": {"id": "enrollment#LF5555#c1", "lf_code": "LF5555", "cohort_id": "c1"},
    }
    result, _ = _run({"action": "list_cohort", "cohort_id": "c1"}, prefills=prefills)
    assert result["cohort_id"] == "c1"
    assert len(result["enrollments"]) == 2


def test_list_cohort_missing_id_raises():
    with pytest.raises(H.HandlerError, match="missing cohort_id"):
        _run({"action": "list_cohort"})


def test_get_participant_history():
    prefills = {
        "enrollment#LF4342#c1": {"id": "enrollment#LF4342#c1", "lf_code": "LF4342", "cohort_id": "c1"},
        "enrollment#LF4342#c2": {"id": "enrollment#LF4342#c2", "lf_code": "LF4342", "cohort_id": "c2"},
    }
    result, _ = _run({"action": "get_participant_history", "lf_code": "LF4342"}, prefills=prefills)
    assert result["lf_code"] == "LF4342"
    assert len(result["history"]) == 2


def test_delete_pre_reservation():
    prefills = {"pre#p@example.com": {"id": "pre#p@example.com", "tcu_code": "LF4342"}}
    result, tbl = _run(
        {"action": "delete_pre_reservation", "email": "p@example.com"},
        prefills=prefills,
    )
    assert result["deleted"] is True
    assert "pre#p@example.com" not in tbl.items


# ---------------------------------------------------------------------------
# Audit identity — caller_identity written from Lambda context, not user input
# ---------------------------------------------------------------------------

def test_pre_reserve_records_caller_identity():
    ctx = FakeContext(arn="arn:aws:iam::123456789:user/dr-smith")
    result, tbl = _run(
        {"action": "pre_reserve", "email": "p@example.com", "role": "LF"},
        ctx=ctx,
    )
    item = tbl.items[f"pre#p@example.com"]
    assert item["reserved_by"] == "arn:aws:iam::123456789:user/dr-smith"


def test_create_cohort_records_caller_identity():
    ctx = FakeContext(arn="arn:aws:iam::123456789:user/dr-smith")
    result, tbl = _run(
        {"action": "create_cohort", "cohort_id": "2026-fall", "name": "Fall 2026"},
        ctx=ctx,
    )
    item = tbl.items["cohort#2026-fall"]
    assert item["created_by"] == "arn:aws:iam::123456789:user/dr-smith"


def test_enroll_participant_records_caller_identity():
    ctx = FakeContext(arn="arn:aws:iam::123456789:user/dr-smith")
    prefills = {"cohort#c1": {"id": "cohort#c1"}}
    result, tbl = _run(
        {"action": "enroll_participant", "lf_code": "LF4342", "cohort_id": "c1"},
        prefills=prefills,
        ctx=ctx,
    )
    item = tbl.items["enrollment#LF4342#c1"]
    assert item["enrolled_by"] == "arn:aws:iam::123456789:user/dr-smith"


def test_update_enrollment_records_caller_identity():
    ctx = FakeContext(arn="arn:aws:iam::123456789:user/dr-smith")
    prefills = {
        "enrollment#LF4342#c1": {
            "id": "enrollment#LF4342#c1",
            "lf_code": "LF4342",
            "cohort_id": "c1",
            "status": "active",
        }
    }
    result, tbl = _run(
        {"action": "update_enrollment_status", "lf_code": "LF4342",
         "cohort_id": "c1", "status": "completed"},
        prefills=prefills,
        ctx=ctx,
    )
    # updated_by is written via DynamoDB update_item; check via result dict
    assert result["status"] == "completed"
    # Verify the DynamoDB item got the updated_by field
    item = tbl.items["enrollment#LF4342#c1"]
    assert item.get("by") == "arn:aws:iam::123456789:user/dr-smith" or \
           item.get("updated_by") == "arn:aws:iam::123456789:user/dr-smith"


def test_delete_pre_reservation_writes_audit_tombstone():
    prefills = {"pre#p@example.com": {"id": "pre#p@example.com", "tcu_code": "LF4342"}}
    ctx = FakeContext(arn="arn:aws:iam::123456789:user/dr-smith")
    _, tbl = _run(
        {"action": "delete_pre_reservation", "email": "p@example.com"},
        prefills=prefills,
        ctx=ctx,
    )
    tombstone = tbl.items.get("audit#delete_pre#p@example.com")
    assert tombstone is not None
    assert tombstone["deleted_by"] == "arn:aws:iam::123456789:user/dr-smith"
    assert tombstone["deleted_code"] == "LF4342"


# ---------------------------------------------------------------------------
# COGNITO_USER_POOL_ID missing → visible error, not silent skip
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RS counter seeding — atomic, never decrements
# ---------------------------------------------------------------------------

def test_seed_rs_counter_creates_when_absent():
    result, tbl = _run({"action": "seed_rs_counter", "minimum": "1"})
    assert result["seeded"] is True
    assert tbl.items["counter#RS"]["rs_next"] == 1


def test_seed_rs_counter_noop_when_already_at_or_above_minimum():
    prefills = {"counter#RS": {"id": "counter#RS", "rs_next": 3}}
    result, tbl = _run({"action": "seed_rs_counter", "minimum": "1"}, prefills=prefills)
    assert tbl.items["counter#RS"]["rs_next"] == 3  # unchanged


def test_seed_rs_counter_raises_when_existing_below_minimum():
    prefills = {"counter#RS": {"id": "counter#RS", "rs_next": 0}}
    with pytest.raises(H.HandlerError, match="less than the requested minimum"):
        _run({"action": "seed_rs_counter", "minimum": "2"}, prefills=prefills)


def test_seed_rs_counter_minimum_zero_raises():
    with pytest.raises(H.HandlerError, match="minimum must be >= 1"):
        _run({"action": "seed_rs_counter", "minimum": "0"})


def test_write_cognito_attributes_raises_when_pool_id_missing():
    """If COGNITO_USER_POOL_ID is unset, the function must raise, not silently
    skip — otherwise participants get no tcu_code and are blocked from recording
    with no actionable error message."""
    with patch.object(H, "COGNITO_USER_POOL_ID", ""):
        with pytest.raises(H.HandlerError, match="COGNITO_USER_POOL_ID"):
            H._write_cognito_attributes("alice", "LF4342", "LF")


# ---------------------------------------------------------------------------
# FakeDdbClient — simulates boto3.client("dynamodb") transact_write_items
# ---------------------------------------------------------------------------

class FakeDdbClient:
    """Minimal boto3 DynamoDB *client* stub supporting transact_write_items.

    Applies each TransactItem against the same FakeTable so test assertions can
    inspect the resulting state through the shared items dict.
    """

    def __init__(self, fake_table: "FakeTable"):
        self._tbl = fake_table

    def transact_write_items(self, TransactItems):
        """Execute each item. Raises TransactionCanceledException if any
        conditional Put/Update fails (simulating all-or-nothing semantics).
        Cancellation reasons list mirrors the real DDB response shape.
        """
        # Dry-run pass: check all conditions first
        failures = []
        for item in TransactItems:
            reason = self._check_condition(item)
            failures.append({"Code": reason} if reason else {})

        any_fail = any(f.get("Code") for f in failures)
        if any_fail:
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "Transaction cancelled",
                    },
                    "CancellationReasons": failures,
                },
                "TransactWriteItems",
            )

        # Apply pass: all conditions passed
        for item in TransactItems:
            self._apply(item)

    def _check_condition(self, item: dict) -> str | None:
        """Return 'ConditionalCheckFailed' string if condition fails, else None."""
        if "Put" in item:
            op = item["Put"]
            cond = op.get("ConditionExpression", "")
            if "attribute_not_exists" in cond:
                pk = op["Item"]["id"]["S"]
                if pk in self._tbl.items:
                    return "ConditionalCheckFailed"
        if "Update" in item:
            op = item["Update"]
            cond = op.get("ConditionExpression", "")
            if "attribute_exists" in cond:
                pk = op["Key"]["id"]["S"]
                if pk not in self._tbl.items:
                    return "ConditionalCheckFailed"
        return None

    def _apply(self, item: dict):
        if "Put" in item:
            raw = item["Put"]["Item"]
            # Unwrap DDB wire format {"S": "..."} → plain Python value
            py_item = {k: self._unwrap(v) for k, v in raw.items()}
            self._tbl.items[py_item["id"]] = py_item

        elif "Update" in item:
            op = item["Update"]
            pk = op["Key"]["id"]["S"]
            current = self._tbl.items.get(pk, {"id": pk})
            expr_vals = {k: self._unwrap(v) for k, v in op.get("ExpressionAttributeValues", {}).items()}
            expr_names = op.get("ExpressionAttributeNames", {})
            # Parse simple "SET a = :a, b = :b" expressions
            set_part = op.get("UpdateExpression", "").replace("SET ", "")
            for clause in set_part.split(","):
                clause = clause.strip()
                if "=" not in clause:
                    continue
                field, placeholder = [x.strip() for x in clause.split("=", 1)]
                # Resolve ExpressionAttributeNames alias (e.g. #rl → role)
                if field in expr_names:
                    field = expr_names[field]
                if placeholder in expr_vals:
                    current[field] = expr_vals[placeholder]
            self._tbl.items[pk] = current

    @staticmethod
    def _unwrap(val):
        if isinstance(val, dict):
            if "S" in val:
                return val["S"]
            if "N" in val:
                return int(val["N"])
            if "BOOL" in val:
                return val["BOOL"]
        return val


# ---------------------------------------------------------------------------
# _normalize_rs_number — input validation
# ---------------------------------------------------------------------------

def test_normalize_rs_number_bare_digits():
    assert H._normalize_rs_number("0006") == "0006"


def test_normalize_rs_number_rs_prefix():
    assert H._normalize_rs_number("RS0006") == "0006"


def test_normalize_rs_number_lowercase():
    assert H._normalize_rs_number("rs0006") == "0006"


def test_normalize_rs_number_no_leading_zeros():
    assert H._normalize_rs_number("6") == "0006"


def test_normalize_rs_number_no_double_prefix():
    """Input 'RS0005' must not produce 'RSRS0005' via any lstrip."""
    n = H._normalize_rs_number("RS0005")
    assert n == "0005"
    assert not n.startswith("RS")


def test_normalize_rs_number_rejects_alpha():
    with pytest.raises(H.HandlerError, match="Invalid RS code"):
        H._normalize_rs_number("RSABC")


def test_normalize_rs_number_rejects_zero():
    with pytest.raises(H.HandlerError, match="out of range"):
        H._normalize_rs_number("0")


def test_normalize_rs_number_rejects_above_max():
    with pytest.raises(H.HandlerError, match="out of range"):
        H._normalize_rs_number("10000")


# ---------------------------------------------------------------------------
# _is_rs_reserved — checks all three record types
# ---------------------------------------------------------------------------

def test_is_rs_reserved_new_prefixed_lock():
    fake = FakeTable({"code#RS0005": {"id": "code#RS0005", "retired": False}})
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0005") is True


def test_is_rs_reserved_legacy_bare_lock():
    fake = FakeTable({"code#0005": {"id": "code#0005"}})
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0005") is True


def test_is_rs_reserved_retired_tombstone():
    fake = FakeTable({"retired#RS0005": {"id": "retired#RS0005"}})
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0005") is True


def test_is_rs_reserved_false_when_no_records():
    fake = FakeTable({})
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0007") is False


def test_is_rs_reserved_accepts_bare_digit_input():
    fake = FakeTable({"code#RS0005": {"id": "code#RS0005"}})
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("0005") is True


# ---------------------------------------------------------------------------
# _next_rs_code — requires seeded counter
# ---------------------------------------------------------------------------

def test_next_rs_code_requires_seeded_counter():
    """Must raise HandlerError when counter#RS does not exist."""
    fake = FakeTable({})
    fake_client = FakeDdbClient(fake)

    # _next_rs_code uses table.update_item; patch it to raise ConditionalCheckFailed
    def raise_conditional(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no item"}},
            "UpdateItem",
        )

    with patch.object(H, "table", fake):
        fake.update_item = raise_conditional
        with pytest.raises(H.HandlerError, match="counter#RS does not exist"):
            H._next_rs_code()


def test_next_rs_code_increments_from_seeded_value():
    fake = FakeTable({"counter#RS": {"id": "counter#RS", "rs_next": 5}})
    with patch.object(H, "table", fake):
        code = H._next_rs_code()
    assert code == "RS0006"


# ---------------------------------------------------------------------------
# _retire_researcher — permanent retirement, never deletes records
# ---------------------------------------------------------------------------

def _retire(sub, email, rs_code, reason="test", prefills=None):
    fake = FakeTable(prefills=prefills or {})
    fake_client = FakeDdbClient(fake)
    with patch.object(H, "table", fake), \
         patch("handler.boto3.client", return_value=fake_client):
        result = H._retire_researcher(sub, email, rs_code, reason, "test-caller")
    return result, fake


def test_retire_writes_tombstone():
    _, tbl = _retire("sub-A", "z@test.com", "RS0005")
    assert "retired#RS0005" in tbl.items


def test_retire_writes_code_rs_lock_when_absent():
    _, tbl = _retire("sub-A", "z@test.com", "RS0005")
    assert "code#RS0005" in tbl.items
    assert tbl.items["code#RS0005"]["retired"] is True


def test_retire_updates_existing_code_rs_lock():
    prefills = {"code#RS0005": {"id": "code#RS0005", "email": "z@test.com"}}
    _, tbl = _retire("sub-A", "z@test.com", "RS0005", prefills=prefills)
    assert tbl.items["code#RS0005"]["retired"] is True
    # Original email field preserved
    assert tbl.items["code#RS0005"]["email"] == "z@test.com"


def test_retire_marks_user_record_deleted():
    prefills = {"user#sub-A": {"id": "user#sub-A", "tcu_code": "RS0005"}}
    _, tbl = _retire("sub-A", "z@test.com", "RS0005", prefills=prefills)
    assert tbl.items["user#sub-A"]["deleted"] is True


def test_retire_does_not_delete_user_record():
    prefills = {"user#sub-A": {"id": "user#sub-A", "tcu_code": "RS0005"}}
    _, tbl = _retire("sub-A", "z@test.com", "RS0005", prefills=prefills)
    # Record still exists; only marked deleted
    assert "user#sub-A" in tbl.items


def test_retire_idempotent_on_existing_tombstone():
    prefills = {"retired#RS0005": {"id": "retired#RS0005", "email": "z@test.com"}}
    result, _ = _retire("sub-A", "z@test.com", "RS0005", prefills=prefills)
    assert result["retired"] is True
    assert result.get("idempotent") is True


def test_retired_code_is_reserved():
    """After retirement, _is_rs_reserved must return True for that slot."""
    prefills = {
        "retired#RS0005": {"id": "retired#RS0005"},
        "code#RS0005": {"id": "code#RS0005", "retired": True},
    }
    fake = FakeTable(prefills)
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0005") is True


# ---------------------------------------------------------------------------
# RS pre-reservation — prefixed namespace, transactional
# ---------------------------------------------------------------------------

def test_rs_pre_reserve_uses_prefixed_code_key():
    result, tbl = _run({
        "action": "pre_reserve",
        "email": "researcher@tcu.edu",
        "role": "RS",
    }, prefills={"counter#RS": {"id": "counter#RS", "rs_next": 5}})
    code = result["tcu_code"]
    n = H._normalize_rs_number(code)
    assert f"code#RS{n}" in tbl.items, "RS pre-reservation must write code#RS<n>, not bare code#<n>"


def test_rs_pre_reserve_does_not_write_bare_code_key():
    result, tbl = _run({
        "action": "pre_reserve",
        "email": "researcher@tcu.edu",
        "role": "RS",
    }, prefills={"counter#RS": {"id": "counter#RS", "rs_next": 5}})
    code = result["tcu_code"]
    n = H._normalize_rs_number(code)
    assert f"code#{n}" not in tbl.items, "must not write legacy bare code#<n> for new RS pre-reservations"


def test_rs_pre_reserve_idempotent():
    prefills = {
        "counter#RS": {"id": "counter#RS", "rs_next": 5},
        "pre#r@tcu.edu": {"id": "pre#r@tcu.edu", "tcu_code": "RS0006"},
    }
    result, _ = _run(
        {"action": "pre_reserve", "email": "r@tcu.edu", "role": "RS"},
        prefills=prefills,
    )
    assert result["tcu_code"] == "RS0006"


# ---------------------------------------------------------------------------
# RS claim — only pre-reserved email can claim its slot
# ---------------------------------------------------------------------------

def test_rs_claim_allows_matching_email():
    prefills = {
        "counter#RS": {"id": "counter#RS", "rs_next": 5},
        "pre#r@tcu.edu": {
            "id": "pre#r@tcu.edu", "tcu_code": "RS0006", "role": "RS", "claimed": False,
        },
        "code#RS0006": {
            "id": "code#RS0006", "pre_reserved": True, "email": "r@tcu.edu",
        },
    }
    result, tbl = _run(
        {"cognito_sub": "sub-R", "cognito_username": "r@tcu.edu", "email": "r@tcu.edu"},
        prefills=prefills,
    )
    assert result["tcu_code"] == "RS0006"
    assert tbl.items["user#sub-R"]["tcu_code"] == "RS0006"


def test_rs_claim_blocks_wrong_email():
    prefills = {
        "code#RS0006": {
            "id": "code#RS0006", "pre_reserved": True, "email": "correct@tcu.edu",
        },
    }
    # _is_rs_claimable_by returns False for wrong email → HandlerError
    fake = FakeTable(prefills)
    with patch.object(H, "table", fake):
        assert H._is_rs_claimable_by("0006", "wrong@tcu.edu") is False
        assert H._is_rs_claimable_by("0006", "correct@tcu.edu") is True


# ---------------------------------------------------------------------------
# Concurrency — counter advances correctly under concurrent assignment
# ---------------------------------------------------------------------------

def test_rs_assign_skips_reserved_slot():
    """When counter#RS emits a code that _is_rs_reserved returns True for,
    the loop must try the next slot rather than double-assigning."""
    # Counter at 5; RS0006 is taken; RS0007 should be assigned
    prefills = {
        "counter#RS": {"id": "counter#RS", "rs_next": 5},
        "code#RS0006": {"id": "code#RS0006", "cognito_sub": "other"},
    }
    result, tbl = _run(
        {"cognito_sub": "sub-new", "cognito_username": "new@tcu.edu", "email": "new@tcu.edu",
         "action": "pre_reserve", "role": "RS"},
        prefills=prefills,
    )
    assert result["tcu_code"] == "RS0007"


# ---------------------------------------------------------------------------
# Rollback — retired IDs can never become available again
# ---------------------------------------------------------------------------

def test_retired_id_cannot_be_reassigned():
    """After retirement, seed_rs_counter at a lower value must still refuse
    to issue the retired code because _is_rs_reserved blocks it."""
    prefills = {
        "retired#RS0005": {"id": "retired#RS0005"},
        "code#RS0005": {"id": "code#RS0005", "retired": True},
        "counter#RS": {"id": "counter#RS", "rs_next": 4},
    }
    fake = FakeTable(prefills)
    # Simulate counter emitting 0005 — _is_rs_reserved must catch it
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0005") is True


def test_legacy_bare_lock_blocks_reissuance():
    """Legacy code#<n> record (no RS prefix) still blocks re-issuance."""
    prefills = {"code#0004": {"id": "code#0004"}}
    fake = FakeTable(prefills)
    with patch.object(H, "table", fake):
        assert H._is_rs_reserved("RS0004") is True
        assert H._is_rs_reserved("0004") is True


# ---------------------------------------------------------------------------
# _migrate_researcher_account — existing-account migration to RS code
# ---------------------------------------------------------------------------

def _migrate(sub, email, rs_code, cognito_username="", prefills=None, cognito_users=None):
    fake = FakeTable(prefills=prefills or {})
    fake_cognito = FakeCognitoIdp(users=cognito_users)
    fake_client = FakeDdbClient(fake)
    with patch.object(H, "table", fake), \
         patch.object(H, "cognito_idp", fake_cognito), \
         patch("handler.boto3.client", return_value=fake_client):
        result = H._migrate_researcher_account(sub, email, rs_code, cognito_username, "test-caller")
    return result, fake, fake_cognito


def _migration_prefills(sub="sub-M", email="mythubanh@gmail.com", old_code="1001"):
    return {
        "user#" + sub: {
            "id": "user#" + sub,
            "tcu_code": old_code,
            "role": "TEST",
            "email": email,
        },
        "pre#" + email: {
            "id": "pre#" + email,
            "tcu_code": "RS0006",
            "email": email,
            "role": "RS",
            "claimed": False,
        },
        "code#RS0006": {
            "id": "code#RS0006",
            "pre_reserved": True,
            "email": email,
            "claimed": False,
        },
    }


def test_migrate_updates_user_record_tcu_code():
    result, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                               prefills=_migration_prefills())
    assert tbl.items["user#sub-M"]["tcu_code"] == "RS0006"


def test_migrate_records_migrated_from():
    result, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                               prefills=_migration_prefills())
    assert tbl.items["user#sub-M"]["migrated_from"] == "1001"


def test_migrate_sets_role_to_rs():
    result, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                               prefills=_migration_prefills())
    assert tbl.items["user#sub-M"]["role"] == "RS"


def test_migrate_marks_pre_reservation_claimed():
    _, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                          prefills=_migration_prefills())
    assert tbl.items["pre#mythubanh@gmail.com"]["claimed"] is True
    assert tbl.items["pre#mythubanh@gmail.com"]["claimed_by_sub"] == "sub-M"


def test_migrate_marks_code_lock_claimed():
    _, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                          prefills=_migration_prefills())
    assert tbl.items["code#RS0006"]["claimed"] is True
    assert tbl.items["code#RS0006"]["claimed_by_sub"] == "sub-M"


def test_migrate_does_not_delete_user_record():
    _, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                          prefills=_migration_prefills())
    assert "user#sub-M" in tbl.items


def test_migrate_preserves_existing_user_fields():
    """Fields beyond tcu_code/role (e.g. enrolled_at_epoch) must survive."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["enrolled_at_epoch"] = 1000000
    _, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)
    assert tbl.items["user#sub-M"]["enrolled_at_epoch"] == 1000000


def test_migrate_writes_cognito_attributes():
    _, _, fake_cognito = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                                   cognito_username="mythubanh@gmail.com",
                                   prefills=_migration_prefills(),
                                   cognito_users=_COGNITO_USERS_OK)
    assert len(fake_cognito.calls) == 1
    attrs = {a["Name"]: a["Value"] for a in fake_cognito.calls[0]["UserAttributes"]}
    assert attrs["custom:tcu_code"] == "RS0006"
    assert attrs["custom:tcu_role"] == "RS"


def test_migrate_skips_cognito_when_username_blank():
    _, _, fake_cognito = _migrate("sub-M", "mythubanh@gmail.com", "RS0006",
                                   cognito_username="",
                                   prefills=_migration_prefills())
    assert len(fake_cognito.calls) == 0


def test_migrate_idempotent_already_migrated():
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"
    prefills["code#RS0006"]["claimed"] = True
    result, _, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)
    assert result["migrated"] is True
    assert result.get("idempotent") is True


def test_migrate_rejects_missing_user_record():
    prefills = _migration_prefills()
    del prefills["user#sub-M"]
    with pytest.raises(H.HandlerError, match="does not exist"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_rejects_missing_code_lock():
    prefills = _migration_prefills()
    del prefills["code#RS0006"]
    with pytest.raises(H.HandlerError, match="does not exist"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_rejects_non_prereserved_lock():
    prefills = _migration_prefills()
    prefills["code#RS0006"]["pre_reserved"] = False
    with pytest.raises(H.HandlerError, match="not marked pre_reserved"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_rejects_email_mismatch_on_lock():
    prefills = _migration_prefills()
    prefills["code#RS0006"]["email"] = "someone_else@gmail.com"
    with pytest.raises(H.HandlerError, match="pre-reserved for a different email"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_rejects_email_mismatch_on_user_record():
    prefills = _migration_prefills()
    prefills["user#sub-M"]["email"] = "other@gmail.com"
    with pytest.raises(H.HandlerError, match="email mismatch"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_accepts_user_record_with_no_email_field():
    """Legacy user# records may not have an email field — should not block migration."""
    prefills = _migration_prefills()
    del prefills["user#sub-M"]["email"]
    result, tbl, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)
    assert result["migrated"] is True
    assert tbl.items["user#sub-M"]["tcu_code"] == "RS0006"


def test_migrate_via_dispatcher():
    prefills = _migration_prefills()
    result, tbl = _run(
        {"action": "migrate_researcher",
         "sub": "sub-M",
         "email": "mythubanh@gmail.com",
         "rs_code": "RS0006",
         "cognito_username": "mythubanh@gmail.com"},
        prefills=prefills,
        cognito_users=_COGNITO_USERS_OK,
    )
    assert result["migrated"] is True
    assert tbl.items["user#sub-M"]["tcu_code"] == "RS0006"


def test_migrate_dispatcher_missing_fields_raises():
    with pytest.raises(H.HandlerError, match="migrate_researcher requires"):
        _run({"action": "migrate_researcher", "sub": "sub-M"})


# ---------------------------------------------------------------------------
# migrate_researcher — Cognito pre-flight ownership verification
# ---------------------------------------------------------------------------

_COGNITO_USERS_OK = {
    "mythubanh@gmail.com": {"sub": "sub-M", "email": "mythubanh@gmail.com"}
}


def test_migrate_cognito_preflight_passes_when_sub_and_email_match():
    """When COGNITO_USER_POOL_ID is set and Cognito confirms sub+email, migration proceeds."""
    result, tbl, _ = _migrate(
        "sub-M", "mythubanh@gmail.com", "RS0006",
        cognito_username="mythubanh@gmail.com",
        prefills=_migration_prefills(),
        cognito_users=_COGNITO_USERS_OK,
    )
    assert result["migrated"] is True
    assert tbl.items["user#sub-M"]["tcu_code"] == "RS0006"


def test_migrate_cognito_preflight_rejects_sub_mismatch():
    """Cognito says username belongs to a different sub — abort before any DDB write."""
    users = {"mythubanh@gmail.com": {"sub": "different-sub", "email": "mythubanh@gmail.com"}}
    with pytest.raises(H.HandlerError, match="Cognito sub mismatch"):
        _migrate(
            "sub-M", "mythubanh@gmail.com", "RS0006",
            cognito_username="mythubanh@gmail.com",
            prefills=_migration_prefills(),
            cognito_users=users,
        )


def test_migrate_cognito_preflight_rejects_email_mismatch():
    """Cognito says username has a different email — abort before any DDB write."""
    users = {"mythubanh@gmail.com": {"sub": "sub-M", "email": "other@gmail.com"}}
    with pytest.raises(H.HandlerError, match="Cognito email mismatch"):
        _migrate(
            "sub-M", "mythubanh@gmail.com", "RS0006",
            cognito_username="mythubanh@gmail.com",
            prefills=_migration_prefills(),
            cognito_users=users,
        )


def test_migrate_cognito_preflight_rejects_unknown_username():
    """Cognito username not found — abort before any DDB write."""
    with pytest.raises(H.HandlerError, match="not found in pool"):
        _migrate(
            "sub-M", "mythubanh@gmail.com", "RS0006",
            cognito_username="mythubanh@gmail.com",
            prefills=_migration_prefills(),
            cognito_users={},  # no users → UserNotFoundException
        )


def test_migrate_cognito_preflight_skipped_when_username_blank():
    """Blank cognito_username skips Cognito pre-flight — DDB-only migration."""
    result, tbl, _ = _migrate(
        "sub-M", "mythubanh@gmail.com", "RS0006",
        cognito_username="",
        prefills=_migration_prefills(),
        cognito_users={},  # would fail if pre-flight ran
    )
    assert result["migrated"] is True


def test_migrate_cognito_preflight_skipped_when_pool_id_missing():
    """When COGNITO_USER_POOL_ID is unset, skip Cognito pre-flight (unit test mode).
    _write_cognito_attributes also requires pool_id, so we patch it out to isolate
    the pre-flight behaviour under test."""
    with patch.object(H, "COGNITO_USER_POOL_ID", ""), \
         patch.object(H, "_write_cognito_attributes"):
        result, tbl, _ = _migrate(
            "sub-M", "mythubanh@gmail.com", "RS0006",
            cognito_username="mythubanh@gmail.com",
            prefills=_migration_prefills(),
            cognito_users={},  # would fail if pre-flight ran
        )
    assert result["migrated"] is True


def test_migrate_rejects_slot_claimed_by_different_sub():
    """RS0006 already claimed by a different sub — abort, never overwrite."""
    prefills = _migration_prefills()
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "other-sub"
    with pytest.raises(H.HandlerError, match="already claimed by sub"):
        _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)


def test_migrate_idempotent_when_slot_claimed_by_same_sub():
    """RS0006 claimed by this exact sub + user# already migrated → idempotent."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "sub-M"
    result, _, _ = _migrate("sub-M", "mythubanh@gmail.com", "RS0006", prefills=prefills)
    assert result["migrated"] is True
    assert result.get("idempotent") is True


def test_migrate_idempotent_repairs_stale_cognito_code():
    """DDB already has RS0006 but Cognito still has the old code.
    Idempotent path must detect the mismatch and repair Cognito."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"   # DDB already migrated
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "sub-M"
    # Cognito has stale tcu_code — simulates DDB-succeeded-but-Cognito-failed scenario
    stale_cognito = {
        "mythubanh@gmail.com": {
            "sub": "sub-M",
            "email": "mythubanh@gmail.com",
            "custom:tcu_code": "1001",   # stale
            "custom:tcu_role": "TEST",
        }
    }
    result, _, fake_cognito = _migrate(
        "sub-M", "mythubanh@gmail.com", "RS0006",
        cognito_username="mythubanh@gmail.com",
        prefills=prefills,
        cognito_users=stale_cognito,
    )
    assert result["migrated"] is True
    assert result.get("idempotent") is True
    assert result.get("cognito_repaired") is True
    # _write_cognito_attributes must have been called to repair
    assert len(fake_cognito.calls) == 1
    written = {a["Name"]: a["Value"] for a in fake_cognito.calls[0]["UserAttributes"]}
    assert written["custom:tcu_code"] == "RS0006"
    assert written["custom:tcu_role"] == "RS"


def test_migrate_idempotent_no_repair_when_cognito_already_correct():
    """DDB and Cognito both have RS0006 — idempotent, no Cognito write needed."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "sub-M"
    already_correct = {
        "mythubanh@gmail.com": {
            "sub": "sub-M",
            "email": "mythubanh@gmail.com",
            "custom:tcu_code": "RS0006",   # already correct
            "custom:tcu_role": "RS",
        }
    }
    result, _, fake_cognito = _migrate(
        "sub-M", "mythubanh@gmail.com", "RS0006",
        cognito_username="mythubanh@gmail.com",
        prefills=prefills,
        cognito_users=already_correct,
    )
    assert result["migrated"] is True
    assert result.get("idempotent") is True
    assert not result.get("cognito_repaired")
    assert len(fake_cognito.calls) == 0   # no write — already consistent


def test_migrate_idempotent_raises_when_cognito_unreachable():
    """DDB already has RS0006 but Cognito is unreachable on retry.
    Must raise HandlerError — never return success when Cognito state is unknown."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "sub-M"

    # Pre-flight succeeds; idempotency branch call fails (simulates transient outage between calls)
    preflight_response = {
        "UserAttributes": [
            {"Name": "sub", "Value": "sub-M"},
            {"Name": "email", "Value": "mythubanh@gmail.com"},
        ]
    }
    service_error = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "endpoint unreachable"}},
        "AdminGetUser",
    )
    unreachable_cognito = MagicMock()
    unreachable_cognito.admin_get_user.side_effect = [preflight_response, service_error]
    unreachable_cognito.admin_update_user_attributes.return_value = {}

    fake = FakeTable(prefills=prefills)
    fake_client = FakeDdbClient(fake)
    with patch.object(H, "table", fake), \
         patch.object(H, "cognito_idp", unreachable_cognito), \
         patch("handler.boto3.client", return_value=fake_client):
        with pytest.raises(H.HandlerError, match="Cognito could not be verified"):
            H._migrate_researcher_account(
                "sub-M", "mythubanh@gmail.com", "RS0006",
                "mythubanh@gmail.com", "test-caller"
            )


def test_migrate_idempotent_no_cognito_verified_flag_when_username_blank():
    """When cognito_username is blank on idempotent path, return cognito_verified=False."""
    prefills = _migration_prefills()
    prefills["user#sub-M"]["tcu_code"] = "RS0006"
    prefills["code#RS0006"]["claimed"] = True
    prefills["code#RS0006"]["claimed_by_sub"] = "sub-M"
    result, _, _ = _migrate(
        "sub-M", "mythubanh@gmail.com", "RS0006",
        cognito_username="",
        prefills=prefills,
    )
    assert result["migrated"] is True
    assert result.get("idempotent") is True
    assert result.get("cognito_verified") is False


def test_migrate_no_ddb_write_on_cognito_preflight_failure():
    """Cognito sub mismatch must abort with zero DynamoDB mutations."""
    users = {"mythubanh@gmail.com": {"sub": "wrong-sub", "email": "mythubanh@gmail.com"}}
    prefills = _migration_prefills()
    original_code = prefills["user#sub-M"]["tcu_code"]
    try:
        _migrate(
            "sub-M", "mythubanh@gmail.com", "RS0006",
            cognito_username="mythubanh@gmail.com",
            prefills=prefills,
            cognito_users=users,
        )
    except H.HandlerError:
        pass
    # user#sub-M must be unchanged
    assert prefills["user#sub-M"]["tcu_code"] == original_code
    assert not prefills["code#RS0006"].get("claimed")
