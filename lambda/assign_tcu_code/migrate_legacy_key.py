#!/usr/bin/env python3
"""Migrate user#mythubanh DynamoDB key to UUID-based user#<sub>.

Background
----------
The original handler keyed DynamoDB records as user#<cognito_sub> using the
value passed in the request payload. The Android app at that time sent the
Cognito username ("mythubanh") instead of the UUID sub. All subsequent accounts
use UUID subs. This script corrects the one legacy record.

What the migration does
-----------------------
Reads three existing DynamoDB items:
  user#mythubanh          — legacy-keyed researcher record
  user#<UUID>             — minimal TEST stub auto-created on first login
  code#1001               — assignment lock record for the researcher code

Merges them into a corrected state:
  user#<UUID>             — single record with all fields from both originals
  user#mythubanh          — deleted (the wrong key is gone)
  code#1001               — cognito_sub corrected from "mythubanh" to UUID

All three writes are executed as a single DynamoDB TransactWriteItems call with
ConditionExpressions on every item. If any item changed between the read and
the write, the entire transaction cancels — no partial state is ever written.

Safety guarantees
-----------------
- Backup is written to disk BEFORE any DynamoDB writes.
- Rollback (--rollback) restores exact original item bytes from that backup.
- Rollback also uses ConditionExpressions to refuse if the live state has
  changed from what migration wrote — no blind overwrites.
- Neither migration nor rollback touches S3, Cognito, or any other table.
- Script is idempotent: re-running after a successful migration reports
  "already migrated" without re-writing.
- If the transaction succeeds but the script crashes before post-checks, the
  migrated state is correct. Re-running detects this and reports success.

Recovery after partial failure
-------------------------------
Transaction is atomic — it either fully commits or fully cancels. If the
script crashes AFTER the transaction but before post-checks:
  1. Re-run the script. It will detect the migrated state and report success.
  2. If you want to revert despite a successful transaction: run --rollback.

If the script crashes BEFORE the transaction (e.g. during backup):
  - No DynamoDB writes occurred. Re-run safely.

Rollback is NOT automatic. You must explicitly pass --rollback and confirm.

Usage
-----
  # Inspect the plan without writing:
  python3 migrate_legacy_key.py --region us-east-2 --dry-run

  # Execute:
  python3 migrate_legacy_key.py --region us-east-2

  # Revert (requires backup file from a prior run):
  python3 migrate_legacy_key.py --region us-east-2 --rollback

  # Backup file location (default):
  lambda/assign_tcu_code/backup_migrate_legacy_key.json

Do not run this script without prior approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants — the specific accounts involved in this migration
# ---------------------------------------------------------------------------
TABLE_NAME = "screenlake-tcu-codes"
LEGACY_KEY = "user#mythubanh"            # wrong key: cognito username used as sub
UUID_SUB   = "81cb6550-3041-709d-540f-2f2237f5987c"
UUID_KEY   = f"user#{UUID_SUB}"          # correct key: UUID sub
CODE_KEY   = "code#1001"                 # assignment lock record for researcher code "1001"
EMAIL      = "mythubanh@gmail.com"
LEGACY_TCU_CODE = "1001"
LEGACY_COGNITO_SUB = "mythubanh"         # wrong value stored in code#1001.cognito_sub

DEFAULT_BACKUP_FILE = os.path.join(
    os.path.dirname(__file__), "backup_migrate_legacy_key.json"
)


# ---------------------------------------------------------------------------
# DynamoDB type helpers
# ---------------------------------------------------------------------------

def _ddb_to_py(val: dict) -> Any:
    """Convert a single DDB typed value ({"S": "x"}) to a Python value."""
    if "S" in val:
        return val["S"]
    if "N" in val:
        return Decimal(val["N"])
    if "BOOL" in val:
        return val["BOOL"]
    if "NULL" in val:
        return None
    if "L" in val:
        return [_ddb_to_py(v) for v in val["L"]]
    if "M" in val:
        return {k: _ddb_to_py(v) for k, v in val["M"].items()}
    return val


def _item_to_py(raw_item: dict) -> dict:
    """Convert a full DDB low-level item ({"field": {"S": "x"}, ...}) to plain Python dict."""
    return {k: _ddb_to_py(v) for k, v in raw_item.items()}


def _py_to_ddb(val: Any) -> dict:
    """Convert a plain Python value to a DDB typed value."""
    if isinstance(val, bool):
        return {"BOOL": val}
    if isinstance(val, str):
        return {"S": val}
    if isinstance(val, (int, float, Decimal)):
        return {"N": str(val)}
    if val is None:
        return {"NULL": True}
    if isinstance(val, list):
        return {"L": [_py_to_ddb(v) for v in val]}
    if isinstance(val, dict):
        return {"M": {k: _py_to_ddb(v) for k, v in val.items()}}
    return {"S": str(val)}


def _item_to_ddb(py_item: dict) -> dict:
    """Convert a plain Python dict to a DDB low-level item."""
    return {k: _py_to_ddb(v) for k, v in py_item.items()}


# ---------------------------------------------------------------------------
# Conflict detection and merge
# ---------------------------------------------------------------------------

def detect_conflicts(legacy: dict, stub: dict) -> list[str]:
    """Return a list of field-level conflict descriptions.

    A conflict is any field (other than 'id') that appears in both items with
    different values. Fields that exist in only one item are always safe to
    merge — they are not conflicts.
    """
    conflicts = []
    for field, legacy_val in legacy.items():
        if field == "id":
            continue
        if field in stub and stub[field] != legacy_val:
            conflicts.append(
                f"  CONFLICT field='{field}': "
                f"legacy={legacy_val!r}, stub={stub[field]!r}"
            )
    return conflicts


def build_merged_item(legacy: dict, stub: dict, epoch: int) -> dict:
    """Build the merged UUID-keyed user record.

    Merge rules:
    - Start with all fields from the legacy record (the authoritative source
      for tcu_code, cognito_username, assigned_at_epoch).
    - Add any fields from the stub that are absent from the legacy record
      (preserves role="TEST" from the stub).
    - Override id to the correct UUID key.
    - Set cognito_sub to the UUID (this field was absent from the legacy record).
    - Add migration audit fields (migrated_from_key, migrated_at_epoch).

    Caller must check detect_conflicts() first; this function does not raise on
    conflicts — it always uses the legacy value as the winner for any overlap.
    """
    merged = dict(legacy)

    # Add fields from stub that are missing from legacy
    for field, value in stub.items():
        if field == "id":
            continue
        if field not in merged:
            merged[field] = value

    # Override/set the correct id and sub
    merged["id"] = UUID_KEY
    merged["cognito_sub"] = UUID_SUB

    # Migration audit trail
    merged["migrated_from_key"] = LEGACY_KEY
    merged["migrated_at_epoch"] = epoch

    return merged


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

class MigrationState:
    NOT_STARTED = "not_started"
    COMPLETE    = "complete"
    INCONSISTENT = "inconsistent"


def detect_state(
    legacy_item: dict | None,
    uuid_item:   dict | None,
    code_item:   dict | None,
) -> tuple[str, str]:
    """Classify the current DynamoDB state.

    Returns (MigrationState, description).

    not_started:  legacy key exists as expected, UUID item is the TEST stub,
                  code#1001.cognito_sub is still "mythubanh"
    complete:     legacy key is gone, UUID item has tcu_code="1001" and
                  migrated_from_key set, code#1001.cognito_sub is UUID
    inconsistent: any other combination — requires manual review
    """
    legacy_exists = legacy_item is not None
    uuid_has_code = uuid_item is not None and uuid_item.get("tcu_code") == LEGACY_TCU_CODE
    uuid_migrated = uuid_item is not None and "migrated_from_key" in uuid_item
    code_has_uuid = (
        code_item is not None and
        code_item.get("cognito_sub") == UUID_SUB
    )
    code_has_legacy = (
        code_item is not None and
        code_item.get("cognito_sub") == LEGACY_COGNITO_SUB
    )

    if legacy_exists and not uuid_has_code and code_has_legacy:
        # Normal pre-migration state
        return MigrationState.NOT_STARTED, "Migration not yet run."

    if not legacy_exists and uuid_has_code and uuid_migrated and code_has_uuid:
        # Normal post-migration state
        return MigrationState.COMPLETE, "Migration already complete (idempotent)."

    desc_parts = []
    if legacy_exists:
        desc_parts.append(f"legacy key {LEGACY_KEY!r} still exists")
    else:
        desc_parts.append(f"legacy key {LEGACY_KEY!r} is gone")
    if uuid_item is not None:
        desc_parts.append(
            f"UUID item tcu_code={uuid_item.get('tcu_code')!r} "
            f"migrated_from_key={'present' if 'migrated_from_key' in uuid_item else 'absent'}"
        )
    else:
        desc_parts.append("UUID item is absent")
    if code_item is not None:
        desc_parts.append(f"code#1001.cognito_sub={code_item.get('cognito_sub')!r}")
    else:
        desc_parts.append("code#1001 is absent")

    return MigrationState.INCONSISTENT, "Inconsistent state: " + "; ".join(desc_parts)


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def run_migration(
    ddb: Any,       # boto3 low-level DynamoDB client
    table_name: str,
    backup_path: str,
    dry_run: bool = False,
) -> dict:
    """Execute the migration. Returns a result dict.

    Separated from main() so tests can inject mock clients directly.
    """
    print(f"Reading current DynamoDB state from table '{table_name}'...")

    legacy_raw = ddb.get_item(TableName=table_name, Key={"id": {"S": LEGACY_KEY}}).get("Item")
    uuid_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": UUID_KEY}}).get("Item")
    code_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": CODE_KEY}}).get("Item")

    legacy_py = _item_to_py(legacy_raw) if legacy_raw else None
    uuid_py   = _item_to_py(uuid_raw)   if uuid_raw   else None
    code_py   = _item_to_py(code_raw)   if code_raw   else None

    print(f"\nCurrent state:")
    print(f"  {LEGACY_KEY}: {json.dumps(legacy_py, default=str) if legacy_py else 'NOT FOUND'}")
    print(f"  {UUID_KEY}:   {json.dumps(uuid_py, default=str) if uuid_py else 'NOT FOUND'}")
    print(f"  {CODE_KEY}:   {json.dumps(code_py, default=str) if code_py else 'NOT FOUND'}")

    # --- State classification ---
    state, state_desc = detect_state(legacy_py, uuid_py, code_py)
    print(f"\nMigration state: {state} — {state_desc}")

    if state == MigrationState.COMPLETE:
        print("Nothing to do.")
        return {"status": "already_complete", "idempotent": True}

    if state == MigrationState.INCONSISTENT:
        print(f"\nERROR: {state_desc}")
        print("Cannot safely proceed. Investigate manually before running migration.")
        return {"status": "error", "reason": state_desc}

    # --- Pre-condition checks ---
    assert legacy_py is not None, "legacy item should exist in NOT_STARTED state"
    assert uuid_py   is not None, "UUID stub should exist in NOT_STARTED state"
    assert code_py   is not None, "code#1001 should exist in NOT_STARTED state"

    if legacy_py.get("tcu_code") != LEGACY_TCU_CODE:
        return {
            "status": "error",
            "reason": (
                f"Expected {LEGACY_KEY}.tcu_code={LEGACY_TCU_CODE!r}, "
                f"got {legacy_py.get('tcu_code')!r}. Investigate."
            ),
        }

    if uuid_py.get("tcu_code") is not None:
        return {
            "status": "error",
            "reason": (
                f"{UUID_KEY} already has tcu_code={uuid_py['tcu_code']!r}. "
                "UUID stub may have been assigned a code by another path. Investigate."
            ),
        }

    if code_py.get("cognito_sub") != LEGACY_COGNITO_SUB:
        return {
            "status": "error",
            "reason": (
                f"Expected {CODE_KEY}.cognito_sub={LEGACY_COGNITO_SUB!r}, "
                f"got {code_py.get('cognito_sub')!r}. Investigate."
            ),
        }

    # --- Conflict analysis ---
    print(f"\n--- Field analysis ---")
    print(f"  Legacy record fields:  {sorted(legacy_py.keys())}")
    print(f"  UUID stub fields:      {sorted(uuid_py.keys())}")

    legacy_only = set(legacy_py) - set(uuid_py) - {"id"}
    stub_only   = set(uuid_py)   - set(legacy_py) - {"id"}
    shared      = (set(legacy_py) & set(uuid_py)) - {"id"}

    if legacy_only:
        print(f"  Fields in legacy only (will be preserved):  {sorted(legacy_only)}")
    if stub_only:
        print(f"  Fields in stub only (will be preserved):    {sorted(stub_only)}")
    if shared:
        print(f"  Fields in both (checking for conflicts):    {sorted(shared)}")

    conflicts = detect_conflicts(legacy_py, uuid_py)
    if conflicts:
        print(f"\n  CONFLICTS detected — cannot proceed:")
        for c in conflicts:
            print(c)
        return {
            "status": "error",
            "reason": f"{len(conflicts)} field conflict(s) require manual resolution",
            "conflicts": conflicts,
        }
    print(f"  No field conflicts. Safe to merge.")

    # --- Build merged item ---
    epoch = int(time.time())
    merged_py = build_merged_item(legacy_py, uuid_py, epoch)
    merged_raw = _item_to_ddb(merged_py)

    print(f"\n--- Migration plan ---")
    print(f"  PUT   {UUID_KEY}:  (merged, {len(merged_py)} fields)")
    print(f"        Fields: {sorted(merged_py.keys())}")
    print(f"  DELETE {LEGACY_KEY}")
    print(f"  UPDATE {CODE_KEY}:  cognito_sub: {LEGACY_COGNITO_SUB!r} → {UUID_SUB!r}")
    print(f"\n  Transaction conditions:")
    print(f"    {UUID_KEY}:   attribute_not_exists(tcu_code) AND #role = 'TEST'")
    print(f"    {LEGACY_KEY}: tcu_code = {LEGACY_TCU_CODE!r} AND attribute_not_exists(migrated_to)")
    print(f"    {CODE_KEY}:   cognito_sub = {LEGACY_COGNITO_SUB!r}")

    if dry_run:
        print(f"\n[dry-run] No DynamoDB writes performed.")
        return {"status": "dry_run", "merged_item": merged_py}

    # --- Save backup before any writes ---
    backup = {
        "backup_created_at_epoch": epoch,
        "items": {
            LEGACY_KEY: legacy_raw,
            UUID_KEY:   uuid_raw,
            CODE_KEY:   code_raw,
        },
    }
    print(f"\nWriting backup to {backup_path} ...")
    with open(backup_path, "w") as f:
        json.dump(backup, f, indent=2, default=str)
    print(f"Backup written.")

    # --- Execute transaction ---
    print(f"\nExecuting DynamoDB transaction...")
    try:
        ddb.transact_write_items(TransactItems=[
            # Item 0: Put merged record into UUID key
            # Condition: UUID stub must still have no tcu_code (not assigned by another path)
            # AND role must still be "TEST" (stub unchanged since our read)
            {
                "Put": {
                    "TableName": table_name,
                    "Item": merged_raw,
                    "ExpressionAttributeNames": {"#role": "role"},
                    "ExpressionAttributeValues": {
                        ":stub_role": {"S": "TEST"},
                    },
                    "ConditionExpression": (
                        "attribute_not_exists(tcu_code) AND #role = :stub_role"
                    ),
                }
            },
            # Item 1: Delete legacy key
            # Condition: tcu_code must be unchanged and not already migrated
            {
                "Delete": {
                    "TableName": table_name,
                    "Key": {"id": {"S": LEGACY_KEY}},
                    "ExpressionAttributeValues": {
                        ":expected_tcu_code": {"S": LEGACY_TCU_CODE},
                    },
                    "ConditionExpression": (
                        "tcu_code = :expected_tcu_code AND "
                        "attribute_not_exists(migrated_to)"
                    ),
                }
            },
            # Item 2: Update code#1001.cognito_sub to UUID
            # Condition: cognito_sub must still be the legacy username
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"id": {"S": CODE_KEY}},
                    "UpdateExpression": "SET cognito_sub = :uuid_sub",
                    "ExpressionAttributeValues": {
                        ":uuid_sub":    {"S": UUID_SUB},
                        ":legacy_sub":  {"S": LEGACY_COGNITO_SUB},
                    },
                    "ConditionExpression": "cognito_sub = :legacy_sub",
                }
            },
        ])
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code", "")
        if err_code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            item_labels = [
                f"{UUID_KEY} (tcu_code appeared or role changed since backup)",
                f"{LEGACY_KEY} (tcu_code changed or migrated_to appeared since backup)",
                f"{CODE_KEY} (cognito_sub changed since backup)",
            ]
            failed = [
                item_labels[i]
                for i, r in enumerate(reasons)
                if r.get("Code") == "ConditionalCheckFailed"
            ]
            msg = (
                "Transaction cancelled — concurrent modification detected:\n  " +
                "\n  ".join(failed) +
                "\nBackup preserved at: " + backup_path +
                "\nRe-read state and verify before retrying."
            )
            return {"status": "cancelled", "reason": msg, "failed_items": failed}
        return {"status": "error", "reason": f"Unexpected DynamoDB error: {e}"}

    print("Transaction committed.")

    # --- Post-checks ---
    print("\nVerifying post-migration state...")
    errors = []

    legacy_after = ddb.get_item(
        TableName=table_name, Key={"id": {"S": LEGACY_KEY}}
    ).get("Item")
    if legacy_after is not None:
        errors.append(f"FAIL: {LEGACY_KEY} still exists after migration")
    else:
        print(f"  OK: {LEGACY_KEY} deleted")

    uuid_after_raw = ddb.get_item(
        TableName=table_name, Key={"id": {"S": UUID_KEY}}
    ).get("Item")
    if uuid_after_raw is None:
        errors.append(f"FAIL: {UUID_KEY} not found after migration")
    else:
        uuid_after = _item_to_py(uuid_after_raw)
        if uuid_after.get("tcu_code") != LEGACY_TCU_CODE:
            errors.append(
                f"FAIL: {UUID_KEY}.tcu_code = {uuid_after.get('tcu_code')!r}, "
                f"expected {LEGACY_TCU_CODE!r}"
            )
        elif uuid_after.get("cognito_sub") != UUID_SUB:
            errors.append(
                f"FAIL: {UUID_KEY}.cognito_sub = {uuid_after.get('cognito_sub')!r}, "
                f"expected {UUID_SUB!r}"
            )
        elif "migrated_from_key" not in uuid_after:
            errors.append(f"FAIL: {UUID_KEY} missing migrated_from_key")
        else:
            print(
                f"  OK: {UUID_KEY} has tcu_code={uuid_after['tcu_code']!r}, "
                f"cognito_sub={uuid_after['cognito_sub']!r}, "
                f"migrated_from_key={uuid_after['migrated_from_key']!r}"
            )

    code_after_raw = ddb.get_item(
        TableName=table_name, Key={"id": {"S": CODE_KEY}}
    ).get("Item")
    if code_after_raw is None:
        errors.append(f"FAIL: {CODE_KEY} not found after migration")
    else:
        code_after = _item_to_py(code_after_raw)
        if code_after.get("cognito_sub") != UUID_SUB:
            errors.append(
                f"FAIL: {CODE_KEY}.cognito_sub = {code_after.get('cognito_sub')!r}, "
                f"expected {UUID_SUB!r}"
            )
        else:
            print(f"  OK: {CODE_KEY}.cognito_sub corrected to UUID")

    if errors:
        for e in errors:
            print(e)
        return {
            "status": "post_check_failed",
            "errors": errors,
            "note": (
                "Transaction committed but post-checks failed. "
                "Re-run to re-verify (may be a transient read issue). "
                "Use --rollback to revert if needed."
            ),
        }

    print("\nMigration complete. All post-checks passed.")
    print(f"Backup retained at: {backup_path}")
    print(f"\nTo rollback if needed:")
    print(f"  python3 migrate_legacy_key.py --region us-east-2 --rollback")
    return {
        "status": "complete",
        "merged_item": merged_py,
    }


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def run_rollback(
    ddb: Any,
    table_name: str,
    backup_path: str,
) -> dict:
    """Restore exact original DynamoDB state from backup.

    Safety conditions (checked before writing):
      - user#UUID must have tcu_code="1001" and migrated_from_key set
        (proves it was written by our migration, not something else)
      - user#mythubanh must not exist
        (migration deleted it; if it's back, something else already restored it)
      - code#1001.cognito_sub must equal the UUID sub
        (migration changed it; if it's already "mythubanh", something else reverted it)

    All three items are written in a single TransactWriteItems call.
    If any condition fails, nothing is written.
    """
    if not os.path.exists(backup_path):
        return {
            "status": "error",
            "reason": (
                f"Backup file not found: {backup_path}\n"
                "Cannot rollback without a backup. "
                "If migration was not run from this machine, locate the backup manually."
            ),
        }

    print(f"Loading backup from {backup_path} ...")
    with open(backup_path) as f:
        backup = json.load(f)

    backup_items = backup.get("items", {})
    legacy_backup_raw = backup_items.get(LEGACY_KEY)
    uuid_backup_raw   = backup_items.get(UUID_KEY)
    code_backup_raw   = backup_items.get(CODE_KEY)

    if not legacy_backup_raw or not uuid_backup_raw or not code_backup_raw:
        return {
            "status": "error",
            "reason": "Backup file is incomplete — one or more items are missing.",
        }

    print(f"Backup created at epoch: {backup.get('backup_created_at_epoch')}")
    print(f"Restoring:")
    print(f"  {LEGACY_KEY}: {json.dumps(_item_to_py(legacy_backup_raw), default=str)}")
    print(f"  {UUID_KEY}:   (restore to original stub)")
    print(f"  {CODE_KEY}:   cognito_sub → {LEGACY_COGNITO_SUB!r}")

    # Read live state before writing
    print(f"\nReading current live state...")
    uuid_live_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": UUID_KEY}}).get("Item")
    legacy_live_raw = ddb.get_item(TableName=table_name, Key={"id": {"S": LEGACY_KEY}}).get("Item")
    code_live_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": CODE_KEY}}).get("Item")

    uuid_live   = _item_to_py(uuid_live_raw)   if uuid_live_raw   else None
    legacy_live = _item_to_py(legacy_live_raw) if legacy_live_raw else None
    code_live   = _item_to_py(code_live_raw)   if code_live_raw   else None

    print(f"  {UUID_KEY}:   {json.dumps(uuid_live, default=str) if uuid_live else 'NOT FOUND'}")
    print(f"  {LEGACY_KEY}: {json.dumps(legacy_live, default=str) if legacy_live else 'NOT FOUND'}")
    print(f"  {CODE_KEY}:   {json.dumps(code_live, default=str) if code_live else 'NOT FOUND'}")

    # Pre-rollback safety checks (informational — ConditionExpressions are the real guard)
    if legacy_live is not None:
        print(f"\nWARNING: {LEGACY_KEY} already exists — migration may have been partially or fully reverted already.")
    if uuid_live is None:
        return {
            "status": "error",
            "reason": f"{UUID_KEY} not found. Cannot rollback — UUID item is missing.",
        }
    if uuid_live.get("tcu_code") != LEGACY_TCU_CODE:
        return {
            "status": "error",
            "reason": (
                f"{UUID_KEY}.tcu_code = {uuid_live.get('tcu_code')!r}, "
                f"expected {LEGACY_TCU_CODE!r}. "
                "UUID item does not match what migration wrote. Investigate."
            ),
        }
    if "migrated_from_key" not in uuid_live:
        return {
            "status": "error",
            "reason": (
                f"{UUID_KEY} is missing migrated_from_key. "
                "Cannot confirm this was written by the migration. Investigate."
            ),
        }

    # Build the restore transaction
    # uuid_backup_raw is the original stub: {"id": {"S": UUID_KEY}, "role": {"S": "TEST"}}
    # We Put this exact item back (replacing all fields — removes the migration fields).

    print(f"\nExecuting rollback transaction...")
    try:
        ddb.transact_write_items(TransactItems=[
            # Item 0: Restore UUID item to original stub
            # Condition: must have tcu_code="1001" (our migration wrote it) and
            # migrated_from_key present (confirms migration provenance)
            {
                "Put": {
                    "TableName": table_name,
                    "Item": uuid_backup_raw,
                    "ExpressionAttributeValues": {
                        ":expected_tcu_code":     {"S": LEGACY_TCU_CODE},
                        ":expected_migrated_from": {"S": LEGACY_KEY},
                    },
                    "ConditionExpression": (
                        "tcu_code = :expected_tcu_code AND "
                        "migrated_from_key = :expected_migrated_from"
                    ),
                }
            },
            # Item 1: Restore legacy key
            # Condition: must not exist (migration deleted it)
            {
                "Put": {
                    "TableName": table_name,
                    "Item": legacy_backup_raw,
                    "ConditionExpression": "attribute_not_exists(id)",
                }
            },
            # Item 2: Restore code#1001.cognito_sub to legacy username
            # Condition: must currently be the UUID (migration changed it)
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"id": {"S": CODE_KEY}},
                    "UpdateExpression": "SET cognito_sub = :legacy_sub",
                    "ExpressionAttributeValues": {
                        ":legacy_sub": {"S": LEGACY_COGNITO_SUB},
                        ":uuid_sub":   {"S": UUID_SUB},
                    },
                    "ConditionExpression": "cognito_sub = :uuid_sub",
                }
            },
        ])
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code", "")
        if err_code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            item_labels = [
                f"{UUID_KEY} (tcu_code or migrated_from_key changed — not our migration's item)",
                f"{LEGACY_KEY} (already exists — another process restored it)",
                f"{CODE_KEY} (cognito_sub is not {UUID_SUB!r} — already reverted or changed)",
            ]
            failed = [
                item_labels[i]
                for i, r in enumerate(reasons)
                if r.get("Code") == "ConditionalCheckFailed"
            ]
            msg = (
                "Rollback transaction cancelled — safety condition failed:\n  " +
                "\n  ".join(failed) +
                "\nInvestigate live state before attempting rollback again."
            )
            return {"status": "cancelled", "reason": msg, "failed_items": failed}
        return {"status": "error", "reason": f"Unexpected DynamoDB error: {e}"}

    print("Rollback transaction committed.")

    # Post-rollback checks
    print("\nVerifying post-rollback state...")
    errors = []

    legacy_after_raw = ddb.get_item(
        TableName=table_name, Key={"id": {"S": LEGACY_KEY}}
    ).get("Item")
    if legacy_after_raw is None:
        errors.append(f"FAIL: {LEGACY_KEY} not found after rollback")
    else:
        legacy_after = _item_to_py(legacy_after_raw)
        if legacy_after.get("tcu_code") != LEGACY_TCU_CODE:
            errors.append(
                f"FAIL: {LEGACY_KEY}.tcu_code = {legacy_after.get('tcu_code')!r}"
            )
        else:
            print(f"  OK: {LEGACY_KEY} restored with tcu_code={LEGACY_TCU_CODE!r}")

    uuid_after_raw = ddb.get_item(
        TableName=table_name, Key={"id": {"S": UUID_KEY}}
    ).get("Item")
    if uuid_after_raw is None:
        errors.append(f"FAIL: {UUID_KEY} not found after rollback")
    else:
        uuid_after = _item_to_py(uuid_after_raw)
        if uuid_after.get("tcu_code") is not None:
            errors.append(
                f"FAIL: {UUID_KEY} still has tcu_code={uuid_after.get('tcu_code')!r} after rollback"
            )
        elif "migrated_from_key" in uuid_after:
            errors.append(
                f"FAIL: {UUID_KEY} still has migrated_from_key after rollback"
            )
        else:
            print(f"  OK: {UUID_KEY} restored to original stub")

    code_after_raw = ddb.get_item(
        TableName=table_name, Key={"id": {"S": CODE_KEY}}
    ).get("Item")
    if code_after_raw is None:
        errors.append(f"FAIL: {CODE_KEY} not found after rollback")
    else:
        code_after = _item_to_py(code_after_raw)
        if code_after.get("cognito_sub") != LEGACY_COGNITO_SUB:
            errors.append(
                f"FAIL: {CODE_KEY}.cognito_sub = {code_after.get('cognito_sub')!r} after rollback"
            )
        else:
            print(f"  OK: {CODE_KEY}.cognito_sub restored to {LEGACY_COGNITO_SUB!r}")

    if errors:
        for e in errors:
            print(e)
        return {
            "status": "post_check_failed",
            "errors": errors,
            "note": "Rollback transaction committed but post-checks failed.",
        }

    print("\nRollback complete. All three items restored to original state.")
    return {"status": "rolled_back"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show plan without writing anything.",
    )
    parser.add_argument(
        "--rollback", action="store_true",
        help="Restore original items from backup. Requires backup file.",
    )
    parser.add_argument(
        "--backup-file", default=DEFAULT_BACKUP_FILE,
        help=f"Path for backup JSON (default: {DEFAULT_BACKUP_FILE})",
    )
    parser.add_argument(
        "--table", default=TABLE_NAME,
        help=f"DynamoDB table name (default: {TABLE_NAME})",
    )
    args = parser.parse_args()

    ddb = boto3.client("dynamodb", region_name=args.region)

    if args.rollback:
        result = run_rollback(ddb, args.table, args.backup_file)
    else:
        result = run_migration(ddb, args.table, args.backup_file, dry_run=args.dry_run)

    status = result.get("status", "unknown")
    if status in ("complete", "already_complete", "dry_run", "rolled_back"):
        sys.exit(0)
    else:
        reason = result.get("reason", "")
        if reason:
            print(f"\nFailed: {reason}")
        sys.exit(1)


if __name__ == "__main__":
    main()
