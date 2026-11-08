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

All three writes are a single TransactWriteItems call. Every item carries a
ConditionExpression that pins ALL backup-snapshot field values, plus checks for
the absence of fields that should not yet exist. This catches concurrent changes
to any field — not just the fields the migration itself modifies.

Safety guarantees
-----------------
- Backup is written to disk BEFORE any DynamoDB writes (atomic temp-rename).
- Existing backup is never overwritten: if backup file already exists the script
  verifies the live DDB state still matches that backup before proceeding.
  If live state diverged, the script refuses and asks you to investigate.
- Rollback verifies that EVERY field in the UUID item still matches what the
  migration wrote. If the account was subsequently updated (e.g. assigned an RS
  code via migrate_researcher), rollback refuses rather than overwriting.
- Neither migration nor rollback touches S3, Cognito, or any other table.
- Idempotent: re-running after a successful migration reports "already complete".
- If the transaction commits but the script crashes before post-checks, re-run
  detects the migrated state and returns "already complete".

Recovery after partial failure
-------------------------------
The transaction is atomic. It either fully commits or fully cancels; partial
writes cannot occur. If the script crashes AFTER the transaction commits:
  1. Re-run. Script detects migrated state → "already_complete". Done.
  2. To revert: run --rollback.
If it crashes BEFORE the transaction (e.g. during backup write):
  No DDB writes occurred. Re-run safely (existing backup is preserved and
  verified against live state before proceeding).

Rollback is NOT automatic. Pass --rollback explicitly.

Usage
-----
  # Inspect the plan without writing:
  python3 migrate_legacy_key.py --region us-east-2 --dry-run

  # Execute:
  python3 migrate_legacy_key.py --region us-east-2

  # Revert (requires backup file from a prior run):
  python3 migrate_legacy_key.py --region us-east-2 --rollback

Do not run this script without prior approval.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants — the specific accounts involved in this migration
# ---------------------------------------------------------------------------
TABLE_NAME         = "screenlake-tcu-codes"
LEGACY_KEY         = "user#mythubanh"
UUID_SUB           = "81cb6550-3041-709d-540f-2f2237f5987c"
UUID_KEY           = f"user#{UUID_SUB}"
CODE_KEY           = "code#1001"
EMAIL              = "mythubanh@gmail.com"
LEGACY_TCU_CODE    = "1001"
LEGACY_COGNITO_SUB = "mythubanh"

DEFAULT_BACKUP_FILE = os.path.join(
    os.path.dirname(__file__), "backup_migrate_legacy_key.json"
)

# Fields the Lambda handler may write to user# records beyond what already
# exists in each backup item.  Absent-field guards are added to the
# ConditionExpression so a concurrent Lambda write to any of these fields
# cancels the transaction before our Put/Delete fires.
UUID_STUB_GUARDED_ABSENT = [
    "tcu_code",             # any assignment path sets this
    "email",                # RS claim via pre-reservation sets this
    "cognito_username",     # assignment paths set this
    "assigned_at_epoch",    # assignment paths set this
    "from_pre_reservation", # pre-reservation claim sets this
    "migrated_from_key",    # our own migration audit field (not yet present)
]

LEGACY_KEY_GUARDED_ABSENT = [
    "role",             # absent in backup; could be set by a handler update
    "email",            # absent in backup; RS claim path sets this
    "from_pre_reservation",
    "migrated_to",      # hypothetical forwarding field
    "deleted",          # retire_researcher sets this
    "deleted_at_epoch",
    "deleted_by",
]

CODE1001_GUARDED_ABSENT = [
    "claimed",
    "claimed_by_sub",
    "claimed_at_epoch",
    "retired",
    "retired_at_epoch",
    "retired_by",
]

# Fields guarded absent when verifying the UUID item during rollback.
# If any of these appear after migration it means a legitimate write happened
# and rollback must be refused.
UUID_MIGRATED_GUARDED_ABSENT = [
    "deleted",
    "deleted_at_epoch",
    "deleted_by",
]


# ---------------------------------------------------------------------------
# DynamoDB type helpers
# ---------------------------------------------------------------------------

def _ddb_to_py(val: dict) -> Any:
    """Convert a single DDB typed value {"S":"x"} to a Python value."""
    if "S"    in val: return val["S"]
    if "N"    in val: return Decimal(val["N"])
    if "BOOL" in val: return val["BOOL"]
    if "NULL" in val: return None
    if "L"    in val: return [_ddb_to_py(v) for v in val["L"]]
    if "M"    in val: return {k: _ddb_to_py(v) for k, v in val["M"].items()}
    return val


def _item_to_py(raw: dict) -> dict:
    return {k: _ddb_to_py(v) for k, v in raw.items()}


def _py_to_ddb(val: Any) -> dict:
    if isinstance(val, bool):    return {"BOOL": val}
    if isinstance(val, str):     return {"S": val}
    if isinstance(val, (int, float, Decimal)): return {"N": str(val)}
    if val is None:              return {"NULL": True}
    if isinstance(val, list):   return {"L": [_py_to_ddb(v) for v in val]}
    if isinstance(val, dict):   return {"M": {k: _py_to_ddb(v) for k, v in val.items()}}
    return {"S": str(val)}


def _item_to_ddb(py_item: dict) -> dict:
    return {k: _py_to_ddb(v) for k, v in py_item.items()}


# ---------------------------------------------------------------------------
# Snapshot condition builder
# ---------------------------------------------------------------------------

def build_snapshot_condition(
    item_py: dict,
    absent_fields: list[str],
) -> tuple[str, dict, dict]:
    """Build a DDB ConditionExpression that pins a full item snapshot.

    For every non-id field present in item_py:
        #f_<field> = :v_<field>   (checks value matches backup)
    For every field in absent_fields:
        attribute_not_exists(#a_<field>)   (checks field not yet written)

    Uses # aliases throughout to avoid DDB reserved-word collisions.

    Returns:
        condition_expr   — string to pass as ConditionExpression
        names            — ExpressionAttributeNames dict
        vals             — ExpressionAttributeValues dict (DDB wire format)
    """
    parts: list[str] = []
    names: dict[str, str] = {}
    vals:  dict[str, dict] = {}

    for field in sorted(item_py):
        if field == "id":
            continue
        name_alias = f"#f_{field}"
        val_alias  = f":v_{field}"
        names[name_alias] = field
        vals[val_alias]   = _py_to_ddb(item_py[field])
        parts.append(f"{name_alias} = {val_alias}")

    for field in absent_fields:
        name_alias = f"#a_{field}"
        names[name_alias] = field
        parts.append(f"attribute_not_exists({name_alias})")

    return " AND ".join(parts), names, vals


# ---------------------------------------------------------------------------
# Conflict detection and merge
# ---------------------------------------------------------------------------

def detect_conflicts(legacy: dict, stub: dict) -> list[str]:
    """Return descriptions of fields that appear in both items with different values.
    id is never a conflict — it always differs by design.
    """
    conflicts = []
    for field, legacy_val in legacy.items():
        if field == "id":
            continue
        if field in stub and stub[field] != legacy_val:
            conflicts.append(
                f"  CONFLICT field={field!r}: "
                f"legacy={legacy_val!r}, stub={stub[field]!r}"
            )
    return conflicts


def build_merged_item(legacy: dict, stub: dict, epoch: int) -> dict:
    """Merge legacy and stub records into the correct UUID-keyed record.

    Rules:
      - All fields from legacy are preserved.
      - Stub-only fields (not in legacy) are added.
      - id is replaced with the UUID key.
      - cognito_sub is set to the UUID.
      - Migration audit fields are appended.

    Call detect_conflicts() first; this function uses the legacy value as the
    winner for any overlap without raising.
    """
    merged = dict(legacy)
    for field, value in stub.items():
        if field == "id":
            continue
        if field not in merged:
            merged[field] = value
    merged["id"]                = UUID_KEY
    merged["cognito_sub"]       = UUID_SUB
    merged["migrated_from_key"] = LEGACY_KEY
    merged["migrated_at_epoch"] = epoch
    return merged


def build_expected_migrated_item(legacy_py: dict, stub_py: dict) -> dict:
    """Reconstruct the expected UUID item state after a successful migration.

    Excludes migrated_at_epoch (timestamp varies by run; cannot be verified
    deterministically in a rollback ConditionExpression).
    """
    merged = build_merged_item(legacy_py, stub_py, epoch=0)
    merged.pop("migrated_at_epoch", None)
    return merged


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

class MigrationState:
    NOT_STARTED  = "not_started"
    COMPLETE     = "complete"
    INCONSISTENT = "inconsistent"


def detect_state(
    legacy_py: dict | None,
    uuid_py:   dict | None,
    code_py:   dict | None,
) -> tuple[str, str]:
    """Classify current DDB state. Returns (MigrationState constant, description)."""
    legacy_exists = legacy_py is not None
    uuid_has_code = uuid_py is not None and uuid_py.get("tcu_code") == LEGACY_TCU_CODE
    uuid_migrated = uuid_py is not None and "migrated_from_key" in uuid_py
    code_has_uuid   = code_py is not None and code_py.get("cognito_sub") == UUID_SUB
    code_has_legacy = code_py is not None and code_py.get("cognito_sub") == LEGACY_COGNITO_SUB

    if legacy_exists and not uuid_has_code and code_has_legacy:
        return MigrationState.NOT_STARTED, "Migration not yet run."

    if not legacy_exists and uuid_has_code and uuid_migrated and code_has_uuid:
        return MigrationState.COMPLETE, "Migration already complete (idempotent)."

    parts = []
    parts.append(f"{LEGACY_KEY!r} {'exists' if legacy_exists else 'absent'}")
    if uuid_py is not None:
        parts.append(
            f"UUID item tcu_code={uuid_py.get('tcu_code')!r} "
            f"migrated_from_key={'present' if 'migrated_from_key' in uuid_py else 'absent'}"
        )
    else:
        parts.append("UUID item absent")
    if code_py is not None:
        parts.append(f"code#1001.cognito_sub={code_py.get('cognito_sub')!r}")
    else:
        parts.append("code#1001 absent")

    return MigrationState.INCONSISTENT, "Inconsistent: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def _write_backup_atomic(backup: dict, path: str) -> None:
    """Write backup JSON atomically via a temp file and os.rename.

    os.rename is atomic on POSIX when src and dst are on the same filesystem.
    The temp file is created in the same directory to ensure this.
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(backup, f, indent=2, default=str)
        os.rename(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _verify_backup_matches_live(
    backup: dict,
    live_legacy: dict | None,
    live_uuid:   dict | None,
    live_code:   dict | None,
) -> list[str]:
    """Compare live DDB items against a loaded backup.

    Returns a list of discrepancy strings (empty → all match).
    Items in both backup and live are compared field-by-field (Python values).
    """
    discrepancies: list[str] = []
    items_to_check = {
        LEGACY_KEY: live_legacy,
        UUID_KEY:   live_uuid,
        CODE_KEY:   live_code,
    }
    for key, live_raw in items_to_check.items():
        backup_raw = backup.get("items", {}).get(key)
        # Presence/absence mismatch
        if backup_raw is None and live_raw is not None:
            discrepancies.append(f"{key}: absent in backup, present live")
            continue
        if backup_raw is not None and live_raw is None:
            discrepancies.append(f"{key}: present in backup, absent live")
            continue
        if backup_raw is None and live_raw is None:
            continue
        # Both present — compare field-by-field
        backup_py = _item_to_py(backup_raw)
        live_py   = _item_to_py(live_raw)
        all_fields = set(backup_py) | set(live_py)
        for field in sorted(all_fields):
            bv = backup_py.get(field)
            lv = live_py.get(field)
            if bv != lv:
                discrepancies.append(
                    f"{key}.{field}: backup={bv!r}, live={lv!r}"
                )
    return discrepancies


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def run_migration(
    ddb: Any,
    table_name: str,
    backup_path: str,
    dry_run: bool = False,
) -> dict:
    """Execute the migration. Returns a result dict with 'status' key.

    Separated from main() so tests can inject mock clients directly.
    """
    print(f"Reading current DynamoDB state from '{table_name}'...")

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

    state, state_desc = detect_state(legacy_py, uuid_py, code_py)
    print(f"\nMigration state: {state} — {state_desc}")

    if state == MigrationState.COMPLETE:
        print("Nothing to do.")
        return {"status": "already_complete", "idempotent": True}

    if state == MigrationState.INCONSISTENT:
        return {"status": "error", "reason": state_desc}

    # --- Structural pre-conditions ---
    assert legacy_py is not None and uuid_py is not None and code_py is not None

    if legacy_py.get("tcu_code") != LEGACY_TCU_CODE:
        return {"status": "error", "reason": (
            f"Expected {LEGACY_KEY}.tcu_code={LEGACY_TCU_CODE!r}, "
            f"got {legacy_py.get('tcu_code')!r}. Investigate."
        )}
    if uuid_py.get("tcu_code") is not None:
        return {"status": "error", "reason": (
            f"{UUID_KEY} already has tcu_code={uuid_py['tcu_code']!r}. "
            "UUID stub may have been assigned a code by another path. Investigate."
        )}
    if code_py.get("cognito_sub") != LEGACY_COGNITO_SUB:
        return {"status": "error", "reason": (
            f"Expected {CODE_KEY}.cognito_sub={LEGACY_COGNITO_SUB!r}, "
            f"got {code_py.get('cognito_sub')!r}. Investigate."
        )}

    # --- Conflict analysis ---
    print(f"\n--- Field analysis ---")
    print(f"  Legacy fields: {sorted(legacy_py)}")
    print(f"  Stub fields:   {sorted(uuid_py)}")
    legacy_only = sorted(set(legacy_py) - set(uuid_py) - {"id"})
    stub_only   = sorted(set(uuid_py)   - set(legacy_py) - {"id"})
    shared      = sorted((set(legacy_py) & set(uuid_py)) - {"id"})
    if legacy_only: print(f"  Legacy-only fields (preserved): {legacy_only}")
    if stub_only:   print(f"  Stub-only fields (preserved):   {stub_only}")
    if shared:      print(f"  Shared fields (conflict-check): {shared}")

    conflicts = detect_conflicts(legacy_py, uuid_py)
    if conflicts:
        for c in conflicts:
            print(c)
        return {"status": "error", "reason": f"{len(conflicts)} conflict(s) require manual resolution", "conflicts": conflicts}
    print("  No conflicts.")

    # --- Build merged item and transaction conditions ---
    epoch = int(time.time())
    merged_py  = build_merged_item(legacy_py, uuid_py, epoch)
    merged_raw = _item_to_ddb(merged_py)

    # Condition for UUID stub Put: ALL backup fields must still match +
    # guarded-absent fields must still be absent.
    uuid_cond, uuid_names, uuid_vals = build_snapshot_condition(
        uuid_py, UUID_STUB_GUARDED_ABSENT
    )

    # Condition for legacy key Delete: ALL backup fields match + guarded-absent.
    legacy_cond, legacy_names, legacy_vals = build_snapshot_condition(
        legacy_py, LEGACY_KEY_GUARDED_ABSENT
    )

    # Condition for code#1001 Update: ALL backup fields match + guarded-absent.
    code_cond, code_names, code_vals = build_snapshot_condition(
        code_py, CODE1001_GUARDED_ABSENT
    )
    # Update expression needs its own alias for the field being changed.
    code_upd_names = {"#u_cognito_sub": "cognito_sub"}
    code_upd_vals  = {":u_cognito_sub": {"S": UUID_SUB}}

    print(f"\n--- Migration plan ---")
    print(f"  PUT    {UUID_KEY}  ({len(merged_py)} fields)")
    print(f"         Snapshot condition pins {len(uuid_py) - 1} field(s) + "
          f"{len(UUID_STUB_GUARDED_ABSENT)} absent-field guard(s)")
    print(f"  DELETE {LEGACY_KEY}")
    print(f"         Snapshot condition pins {len(legacy_py) - 1} field(s) + "
          f"{len(LEGACY_KEY_GUARDED_ABSENT)} absent-field guard(s)")
    print(f"  UPDATE {CODE_KEY}  SET cognito_sub → {UUID_SUB!r}")
    print(f"         Snapshot condition pins {len(code_py) - 1} field(s) + "
          f"{len(CODE1001_GUARDED_ABSENT)} absent-field guard(s)")

    if dry_run:
        print(f"\n[dry-run] No DynamoDB writes performed.")
        return {"status": "dry_run", "merged_item": merged_py}

    # --- Backup before any writes ---
    backup = {
        "backup_created_at_epoch": epoch,
        "items": {
            LEGACY_KEY: legacy_raw,
            UUID_KEY:   uuid_raw,
            CODE_KEY:   code_raw,
        },
    }

    if os.path.exists(backup_path):
        print(f"\nBackup file already exists at {backup_path}.")
        print("Verifying live state matches that backup before reusing it...")
        with open(backup_path) as f:
            existing_backup = json.load(f)
        discrepancies = _verify_backup_matches_live(
            existing_backup, legacy_raw, uuid_raw, code_raw
        )
        if discrepancies:
            msg = (
                f"Live DDB state differs from existing backup:\n" +
                "\n".join(f"  {d}" for d in discrepancies) +
                f"\nBackup file: {backup_path}\n"
                "Investigate before proceeding. Do not delete the backup without understanding why."
            )
            return {"status": "error", "reason": msg}
        print("  Live state matches backup. Reusing existing backup.")
    else:
        print(f"\nWriting backup to {backup_path} ...")
        _write_backup_atomic(backup, backup_path)
        print("Backup written.")

    # --- Execute transaction ---
    print(f"\nExecuting DynamoDB transaction...")
    try:
        ddb.transact_write_items(TransactItems=[
            {
                "Put": {
                    "TableName": table_name,
                    "Item": merged_raw,
                    "ConditionExpression": uuid_cond,
                    "ExpressionAttributeNames":  uuid_names,
                    "ExpressionAttributeValues": uuid_vals,
                }
            },
            {
                "Delete": {
                    "TableName": table_name,
                    "Key": {"id": {"S": LEGACY_KEY}},
                    "ConditionExpression": legacy_cond,
                    "ExpressionAttributeNames":  legacy_names,
                    "ExpressionAttributeValues": legacy_vals,
                }
            },
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"id": {"S": CODE_KEY}},
                    "UpdateExpression": "SET #u_cognito_sub = :u_cognito_sub",
                    "ConditionExpression": code_cond,
                    "ExpressionAttributeNames":  {**code_names, **code_upd_names},
                    "ExpressionAttributeValues": {**code_vals, **code_upd_vals},
                }
            },
        ])
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code", "")
        if err_code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            item_labels = [
                f"{UUID_KEY} (a field changed or unexpected field appeared since backup)",
                f"{LEGACY_KEY} (a field changed or unexpected field appeared since backup)",
                f"{CODE_KEY} (a field changed or unexpected field appeared since backup)",
            ]
            failed = [
                item_labels[i]
                for i, r in enumerate(reasons)
                if r.get("Code") == "ConditionalCheckFailed"
            ]
            return {
                "status": "cancelled",
                "reason": (
                    "Transaction cancelled — concurrent modification detected:\n  " +
                    "\n  ".join(failed) +
                    f"\nBackup preserved at: {backup_path}\n"
                    "Re-read the live state and verify before retrying."
                ),
                "failed_items": failed,
            }
        return {"status": "error", "reason": f"Unexpected DynamoDB error: {e}"}

    print("Transaction committed.")

    # --- Post-checks ---
    print("\nVerifying post-migration state...")
    errors = []

    if ddb.get_item(TableName=table_name, Key={"id": {"S": LEGACY_KEY}}).get("Item"):
        errors.append(f"FAIL: {LEGACY_KEY} still exists")
    else:
        print(f"  OK: {LEGACY_KEY} deleted")

    uuid_after = _item_to_py(ddb.get_item(TableName=table_name, Key={"id": {"S": UUID_KEY}}).get("Item") or {})
    if not uuid_after:
        errors.append(f"FAIL: {UUID_KEY} not found")
    elif uuid_after.get("tcu_code") != LEGACY_TCU_CODE:
        errors.append(f"FAIL: {UUID_KEY}.tcu_code={uuid_after.get('tcu_code')!r}")
    elif uuid_after.get("cognito_sub") != UUID_SUB:
        errors.append(f"FAIL: {UUID_KEY}.cognito_sub={uuid_after.get('cognito_sub')!r}")
    elif "migrated_from_key" not in uuid_after:
        errors.append(f"FAIL: {UUID_KEY} missing migrated_from_key")
    else:
        print(f"  OK: {UUID_KEY} has tcu_code, cognito_sub, migrated_from_key")

    code_after = _item_to_py(ddb.get_item(TableName=table_name, Key={"id": {"S": CODE_KEY}}).get("Item") or {})
    if not code_after:
        errors.append(f"FAIL: {CODE_KEY} not found")
    elif code_after.get("cognito_sub") != UUID_SUB:
        errors.append(f"FAIL: {CODE_KEY}.cognito_sub={code_after.get('cognito_sub')!r}")
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
                "Re-run to re-verify (may be transient). "
                "Use --rollback to revert if needed."
            ),
        }

    print(f"\nMigration complete. All post-checks passed.")
    print(f"Backup retained at: {backup_path}")
    print(f"\nTo rollback if needed:")
    print(f"  python3 migrate_legacy_key.py --region us-east-2 --rollback")
    return {"status": "complete", "merged_item": merged_py}


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def run_rollback(
    ddb: Any,
    table_name: str,
    backup_path: str,
) -> dict:
    """Restore exact original DynamoDB state from backup.

    Rollback conditions verify that the live state still matches what the
    migration wrote — not just the migration-provenance fields (tcu_code,
    migrated_from_key) but ALL fields that migration touched. If any field
    was subsequently modified by a legitimate write (e.g. migrate_researcher
    updated tcu_code to RS0006, retire_researcher added deleted=True), the
    corresponding condition fails and rollback is refused atomically.
    """
    if not os.path.exists(backup_path):
        return {"status": "error", "reason": (
            f"Backup file not found: {backup_path}\n"
            "Cannot rollback without a backup."
        )}

    print(f"Loading backup from {backup_path} ...")
    with open(backup_path) as f:
        backup = json.load(f)

    backup_items = backup.get("items", {})
    legacy_backup_raw = backup_items.get(LEGACY_KEY)
    uuid_backup_raw   = backup_items.get(UUID_KEY)
    code_backup_raw   = backup_items.get(CODE_KEY)

    if not legacy_backup_raw or not uuid_backup_raw or not code_backup_raw:
        return {"status": "error", "reason": "Backup file is incomplete — one or more items missing."}

    legacy_backup_py = _item_to_py(legacy_backup_raw)
    uuid_backup_py   = _item_to_py(uuid_backup_raw)
    code_backup_py   = _item_to_py(code_backup_raw)

    print(f"Backup created at epoch: {backup.get('backup_created_at_epoch')}")
    print(f"Will restore:")
    print(f"  {LEGACY_KEY}: {json.dumps(legacy_backup_py, default=str)}")
    print(f"  {UUID_KEY}:   (restore to original stub {json.dumps(uuid_backup_py, default=str)})")
    print(f"  {CODE_KEY}:   cognito_sub → {LEGACY_COGNITO_SUB!r}")

    # --- Read live state ---
    print(f"\nReading current live state...")
    uuid_live_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": UUID_KEY}}).get("Item")
    legacy_live_raw = ddb.get_item(TableName=table_name, Key={"id": {"S": LEGACY_KEY}}).get("Item")
    code_live_raw   = ddb.get_item(TableName=table_name, Key={"id": {"S": CODE_KEY}}).get("Item")

    uuid_live = _item_to_py(uuid_live_raw) if uuid_live_raw else None
    print(f"  {UUID_KEY}:   {json.dumps(uuid_live, default=str) if uuid_live else 'NOT FOUND'}")
    print(f"  {LEGACY_KEY}: {'present' if legacy_live_raw else 'NOT FOUND'}")
    print(f"  {CODE_KEY}:   {json.dumps(_item_to_py(code_live_raw), default=str) if code_live_raw else 'NOT FOUND'}")

    if uuid_live is None:
        return {"status": "error", "reason": f"{UUID_KEY} not found. Cannot rollback."}
    if uuid_live.get("tcu_code") != LEGACY_TCU_CODE:
        return {"status": "error", "reason": (
            f"{UUID_KEY}.tcu_code={uuid_live.get('tcu_code')!r}, "
            f"expected {LEGACY_TCU_CODE!r}. Item may have been updated since migration. Investigate."
        )}
    if "migrated_from_key" not in uuid_live:
        return {"status": "error", "reason": (
            f"{UUID_KEY} is missing migrated_from_key. "
            "Cannot confirm this was written by the migration."
        )}

    # --- Build conditions for rollback transaction ---

    # Rollback UUID item condition: pin ALL fields that migration wrote
    # (excluding migrated_at_epoch which varies and cannot be reproduced exactly).
    # + absent-field guards for fields that would indicate a legitimate post-migration write.
    expected_migrated = build_expected_migrated_item(legacy_backup_py, uuid_backup_py)
    rbk_uuid_cond, rbk_uuid_names, rbk_uuid_vals = build_snapshot_condition(
        expected_migrated, UUID_MIGRATED_GUARDED_ABSENT
    )

    # Rollback legacy item condition: must still not exist (migration deleted it).
    # (Simple condition; ConditionExpression on the Put operation handles this.)

    # Rollback code#1001 condition: pin ALL fields of the post-migration state
    # (backup fields + corrected cognito_sub) + guarded-absent for claimed/retired.
    expected_code_migrated = {**code_backup_py, "cognito_sub": UUID_SUB}
    rbk_code_cond, rbk_code_names, rbk_code_vals = build_snapshot_condition(
        expected_code_migrated, CODE1001_GUARDED_ABSENT
    )
    rbk_code_upd_names = {"#r_cognito_sub": "cognito_sub"}
    rbk_code_upd_vals  = {":r_cognito_sub": {"S": LEGACY_COGNITO_SUB}}

    print(f"\nExecuting rollback transaction...")
    try:
        ddb.transact_write_items(TransactItems=[
            # Restore UUID item to original stub content.
            # Condition pins all migration-written fields + guards absent fields.
            {
                "Put": {
                    "TableName": table_name,
                    "Item": uuid_backup_raw,
                    "ConditionExpression": rbk_uuid_cond,
                    "ExpressionAttributeNames":  rbk_uuid_names,
                    "ExpressionAttributeValues": rbk_uuid_vals,
                }
            },
            # Restore legacy key — must not exist (migration deleted it).
            {
                "Put": {
                    "TableName": table_name,
                    "Item": legacy_backup_raw,
                    "ConditionExpression": "attribute_not_exists(id)",
                }
            },
            # Restore code#1001.cognito_sub.
            # Condition pins all post-migration fields + guards claimed/retired.
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {"id": {"S": CODE_KEY}},
                    "UpdateExpression": "SET #r_cognito_sub = :r_cognito_sub",
                    "ConditionExpression": rbk_code_cond,
                    "ExpressionAttributeNames":  {**rbk_code_names, **rbk_code_upd_names},
                    "ExpressionAttributeValues": {**rbk_code_vals, **rbk_code_upd_vals},
                }
            },
        ])
    except ClientError as e:
        err_code = e.response.get("Error", {}).get("Code", "")
        if err_code == "TransactionCanceledException":
            reasons = e.response.get("CancellationReasons", [])
            item_labels = [
                f"{UUID_KEY} (fields changed since migration — legitimate post-migration write?)",
                f"{LEGACY_KEY} (already exists — another process restored it?)",
                f"{CODE_KEY} (fields changed since migration — claimed or retired?)",
            ]
            failed = [
                item_labels[i]
                for i, r in enumerate(reasons)
                if r.get("Code") == "ConditionalCheckFailed"
            ]
            return {
                "status": "cancelled",
                "reason": (
                    "Rollback refused — safety condition failed:\n  " +
                    "\n  ".join(failed) +
                    "\nInvestigate live state before attempting rollback."
                ),
                "failed_items": failed,
            }
        return {"status": "error", "reason": f"Unexpected DynamoDB error: {e}"}

    print("Rollback transaction committed.")

    # --- Post-rollback checks ---
    print("\nVerifying post-rollback state...")
    errors = []

    legacy_after = _item_to_py(ddb.get_item(TableName=table_name, Key={"id": {"S": LEGACY_KEY}}).get("Item") or {})
    if not legacy_after:
        errors.append(f"FAIL: {LEGACY_KEY} not found")
    elif legacy_after.get("tcu_code") != LEGACY_TCU_CODE:
        errors.append(f"FAIL: {LEGACY_KEY}.tcu_code={legacy_after.get('tcu_code')!r}")
    else:
        print(f"  OK: {LEGACY_KEY} restored with tcu_code={LEGACY_TCU_CODE!r}")

    uuid_after = _item_to_py(ddb.get_item(TableName=table_name, Key={"id": {"S": UUID_KEY}}).get("Item") or {})
    if not uuid_after:
        errors.append(f"FAIL: {UUID_KEY} not found")
    elif uuid_after.get("tcu_code") is not None:
        errors.append(f"FAIL: {UUID_KEY} still has tcu_code after rollback")
    elif "migrated_from_key" in uuid_after:
        errors.append(f"FAIL: {UUID_KEY} still has migrated_from_key after rollback")
    else:
        print(f"  OK: {UUID_KEY} restored to original stub")

    code_after = _item_to_py(ddb.get_item(TableName=table_name, Key={"id": {"S": CODE_KEY}}).get("Item") or {})
    if not code_after:
        errors.append(f"FAIL: {CODE_KEY} not found")
    elif code_after.get("cognito_sub") != LEGACY_COGNITO_SUB:
        errors.append(f"FAIL: {CODE_KEY}.cognito_sub={code_after.get('cognito_sub')!r}")
    else:
        print(f"  OK: {CODE_KEY}.cognito_sub restored to {LEGACY_COGNITO_SUB!r}")

    if errors:
        for e in errors:
            print(e)
        return {"status": "post_check_failed", "errors": errors}

    print("\nRollback complete.")
    return {"status": "rolled_back"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--region",      default="us-east-2")
    parser.add_argument("--dry-run",     action="store_true")
    parser.add_argument("--rollback",    action="store_true")
    parser.add_argument("--backup-file", default=DEFAULT_BACKUP_FILE)
    parser.add_argument("--table",       default=TABLE_NAME)
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
