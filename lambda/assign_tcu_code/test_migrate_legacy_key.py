"""Tests for migrate_legacy_key.py.

Covers:
  - Field analysis (conflict detection, merge correctness)
  - Migration: pre-condition failures, dry-run, transaction commit, post-checks
  - Transaction cancellation on each condition (concurrent modification)
  - Idempotency: already-migrated state detected without re-writing
  - Inconsistent state detection
  - Rollback: safety conditions, transaction commit, post-checks
  - Rollback cancellation on each condition
  - Crash recovery: transaction committed but script crashed before post-checks
  - Repeated migration execution
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from migrate_legacy_key import (
    LEGACY_COGNITO_SUB,
    LEGACY_KEY,
    LEGACY_TCU_CODE,
    UUID_KEY,
    UUID_SUB,
    CODE_KEY,
    EMAIL,
    MigrationState,
    build_merged_item,
    detect_conflicts,
    detect_state,
    run_migration,
    run_rollback,
    _item_to_ddb,
    _item_to_py,
    _py_to_ddb,
)

# ---------------------------------------------------------------------------
# Canonical test fixtures (plain Python, not DDB wire format)
# ---------------------------------------------------------------------------

LEGACY_ITEM_PY = {
    "id": LEGACY_KEY,
    "tcu_code": LEGACY_TCU_CODE,
    "cognito_username": EMAIL,
    "assigned_at_epoch": Decimal("1791177470"),
}

UUID_STUB_PY = {
    "id": UUID_KEY,
    "role": "TEST",
}

CODE_ITEM_PY = {
    "id": CODE_KEY,
    "cognito_sub": LEGACY_COGNITO_SUB,
    "cognito_username": EMAIL,
    "assigned_at_epoch": Decimal("1791177470"),
}

EPOCH = 1800000000


def _ddb(py_item):
    """Convert a plain Python item to DDB wire format."""
    return _item_to_ddb(py_item)


def _py(ddb_item):
    """Convert a DDB wire-format item to plain Python."""
    return _item_to_py(ddb_item)


# ---------------------------------------------------------------------------
# FakeDdb — in-memory DynamoDB client for transaction testing
# ---------------------------------------------------------------------------

class _TransactionCancelled(Exception):
    def __init__(self, reasons):
        self.reasons = reasons
        super().__init__("TransactionCanceledException")


class FakeDdb:
    """Minimal in-memory DynamoDB low-level client."""

    def __init__(self, items: dict[str, dict]):
        # items: {pk_string: ddb_wire_item_or_None}
        self._items = {k: copy.deepcopy(v) for k, v in items.items() if v is not None}

    # --- Low-level API surface used by migrate_legacy_key ---

    def get_item(self, TableName, Key):
        pk = _py(Key)["id"]
        item = self._items.get(pk)
        return {"Item": copy.deepcopy(item)} if item else {}

    def transact_write_items(self, TransactItems):
        # Evaluate conditions first; commit only if ALL pass
        results: list[dict | None] = []
        for op in TransactItems:
            kind = next(iter(op))
            spec = op[kind]
            pk = _py(spec["Key"])["id"] if kind != "Put" and kind != "Delete" else _py(spec["Item"] if kind == "Put" else spec["Key"])["id"]
            existing = self._items.get(pk)
            cond = spec.get("ConditionExpression", "")
            expr_vals = {k: _py({k: v})[k] for k, v in spec.get("ExpressionAttributeValues", {}).items()}
            expr_names = spec.get("ExpressionAttributeNames", {})

            failed = not self._eval_condition(cond, existing, expr_vals, expr_names)
            results.append({"Code": "ConditionalCheckFailed"} if failed else {"Code": "None"})

        if any(r["Code"] == "ConditionalCheckFailed" for r in results):
            err = MagicMock()
            err.response = {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": results,
            }
            from botocore.exceptions import ClientError
            raise ClientError(err.response, "TransactWriteItems")

        # All conditions passed — apply mutations
        for op in TransactItems:
            kind = next(iter(op))
            spec = op[kind]
            expr_vals = {k: _py({k: v})[k] for k, v in spec.get("ExpressionAttributeValues", {}).items()}

            if kind == "Put":
                item = _py(spec["Item"])
                pk = item["id"]
                self._items[pk] = copy.deepcopy(spec["Item"])
            elif kind == "Delete":
                pk = _py(spec["Key"])["id"]
                self._items.pop(pk, None)
            elif kind == "Update":
                pk = _py(spec["Key"])["id"]
                update_expr = spec.get("UpdateExpression", "")
                # Support "SET a = :a, b = :b" only
                if update_expr.startswith("SET "):
                    for assignment in update_expr[4:].split(","):
                        assignment = assignment.strip()
                        field, placeholder = [s.strip() for s in assignment.split("=", 1)]
                        if pk not in self._items:
                            self._items[pk] = {"id": spec["Key"]["id"]}
                        self._items[pk][field] = _py_to_ddb(expr_vals[placeholder])

    def _eval_condition(self, cond: str, existing: dict | None, expr_vals: dict, expr_names: dict) -> bool:
        """Evaluate a DDB ConditionExpression. Supports a subset of operators."""
        if not cond:
            return True

        existing_py = _item_to_py(existing) if existing else {}

        def resolve_name(name: str) -> str:
            return expr_names.get(name, name)

        def eval_atom(atom: str) -> bool:
            atom = atom.strip()
            if "attribute_not_exists(" in atom:
                field = atom.split("(", 1)[1].rstrip(")")
                field = resolve_name(field)
                return field not in existing_py
            if "attribute_exists(" in atom:
                field = atom.split("(", 1)[1].rstrip(")")
                field = resolve_name(field)
                return field in existing_py
            if "=" in atom and "attribute" not in atom:
                parts = [p.strip() for p in atom.split("=", 1)]
                if len(parts) == 2:
                    lhs, rhs = parts
                    lhs = resolve_name(lhs)
                    if rhs in expr_vals:
                        return existing_py.get(lhs) == expr_vals[rhs]
            return True

        def eval_expr(expr: str) -> bool:
            expr = expr.strip()
            # Strip outer parens if fully wrapped
            if expr.startswith("(") and expr.endswith(")"):
                depth = 0
                fully_wrapped = True
                for i, ch in enumerate(expr):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                    if depth == 0 and i < len(expr) - 1:
                        fully_wrapped = False
                        break
                if fully_wrapped:
                    expr = expr[1:-1].strip()

            # Split on AND (depth-aware)
            depth = 0
            parts = []
            current = []
            i = 0
            while i < len(expr):
                ch = expr[i]
                if ch == "(":
                    depth += 1
                    current.append(ch)
                elif ch == ")":
                    depth -= 1
                    current.append(ch)
                elif depth == 0 and expr[i:i+4].upper() == " AND":
                    parts.append("".join(current).strip())
                    current = []
                    i += 4
                    continue
                else:
                    current.append(ch)
                i += 1
            if current:
                parts.append("".join(current).strip())

            # Each AND part may itself be "(A OR B)"
            for part in parts:
                part = part.strip()
                if part.startswith("(") and " OR " in part.upper():
                    inner = part.strip("()")
                    or_parts = [p.strip() for p in inner.split(" OR ")]
                    if not any(eval_atom(p) for p in or_parts):
                        return False
                else:
                    if not eval_atom(part):
                        return False
            return True

        return eval_expr(cond)


# ---------------------------------------------------------------------------
# Helper: build a pre-migration FakeDdb
# ---------------------------------------------------------------------------

def _pre_migration_ddb(**overrides) -> FakeDdb:
    items = {
        LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
        UUID_KEY:   _ddb(UUID_STUB_PY),
        CODE_KEY:   _ddb(CODE_ITEM_PY),
    }
    items.update(overrides)
    return FakeDdb(items)


def _post_migration_ddb() -> FakeDdb:
    merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
    items = {
        UUID_KEY:  _ddb(merged),
        CODE_KEY:  _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
    }
    return FakeDdb(items)


# ---------------------------------------------------------------------------
# Field analysis tests
# ---------------------------------------------------------------------------

class TestFieldAnalysis(unittest.TestCase):

    def test_no_conflicts_for_actual_records(self):
        """Live data: legacy and stub have disjoint fields — no conflicts."""
        conflicts = detect_conflicts(LEGACY_ITEM_PY, UUID_STUB_PY)
        self.assertEqual(conflicts, [], f"Unexpected conflicts: {conflicts}")

    def test_conflict_detected_when_shared_field_differs(self):
        """If the same field appears in both items with different values, it is a conflict."""
        legacy_with_role = {**LEGACY_ITEM_PY, "role": "LF"}
        stub_with_role   = {**UUID_STUB_PY,   "role": "TEST"}
        conflicts = detect_conflicts(legacy_with_role, stub_with_role)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("role", conflicts[0])

    def test_no_conflict_when_shared_field_identical(self):
        """Same field same value → not a conflict."""
        legacy_with_role = {**LEGACY_ITEM_PY, "role": "TEST"}
        stub_with_role   = {**UUID_STUB_PY,   "role": "TEST"}
        conflicts = detect_conflicts(legacy_with_role, stub_with_role)
        self.assertEqual(conflicts, [])

    def test_id_field_never_flagged_as_conflict(self):
        """id always differs between records but should never be flagged."""
        conflicts = detect_conflicts(
            {"id": LEGACY_KEY, "foo": "bar"},
            {"id": UUID_KEY,   "foo": "bar"},
        )
        self.assertEqual(conflicts, [])

    def test_merged_item_contains_all_legacy_fields(self):
        """Every non-id field from the legacy record must appear in merged item."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        for field, value in LEGACY_ITEM_PY.items():
            if field == "id":
                continue
            self.assertIn(field, merged, f"Missing field {field!r}")
            self.assertEqual(merged[field], value, f"Field {field!r} value mismatch")

    def test_merged_item_contains_stub_only_fields(self):
        """Fields that only exist in the stub must be preserved."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertIn("role", merged)
        self.assertEqual(merged["role"], "TEST")

    def test_merged_item_has_correct_id_and_cognito_sub(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertEqual(merged["id"], UUID_KEY)
        self.assertEqual(merged["cognito_sub"], UUID_SUB)

    def test_merged_item_has_audit_fields(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertEqual(merged["migrated_from_key"], LEGACY_KEY)
        self.assertEqual(merged["migrated_at_epoch"], EPOCH)

    def test_legacy_wins_on_field_conflict(self):
        """When both records share a field with different values, legacy value is used."""
        legacy_with_role = {**LEGACY_ITEM_PY, "role": "LF"}
        stub_with_role   = {**UUID_STUB_PY,   "role": "TEST"}
        merged = build_merged_item(legacy_with_role, stub_with_role, EPOCH)
        self.assertEqual(merged["role"], "LF")


# ---------------------------------------------------------------------------
# State detection tests
# ---------------------------------------------------------------------------

class TestDetectState(unittest.TestCase):

    def test_not_started(self):
        state, _ = detect_state(LEGACY_ITEM_PY, UUID_STUB_PY, CODE_ITEM_PY)
        self.assertEqual(state, MigrationState.NOT_STARTED)

    def test_complete(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        code_migrated = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB}
        state, _ = detect_state(None, merged, code_migrated)
        self.assertEqual(state, MigrationState.COMPLETE)

    def test_inconsistent_legacy_gone_uuid_not_migrated(self):
        state, _ = detect_state(None, UUID_STUB_PY, CODE_ITEM_PY)
        self.assertEqual(state, MigrationState.INCONSISTENT)

    def test_inconsistent_legacy_exists_uuid_has_code(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        state, _ = detect_state(LEGACY_ITEM_PY, merged, CODE_ITEM_PY)
        self.assertEqual(state, MigrationState.INCONSISTENT)

    def test_inconsistent_missing_code_item(self):
        state, _ = detect_state(LEGACY_ITEM_PY, UUID_STUB_PY, None)
        self.assertEqual(state, MigrationState.INCONSISTENT)


# ---------------------------------------------------------------------------
# Migration: dry-run
# ---------------------------------------------------------------------------

class TestMigrationDryRun(unittest.TestCase):

    def _run(self, **ddb_overrides):
        fake = _pre_migration_ddb(**ddb_overrides)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        os.unlink(backup_path)
        try:
            return run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=True), fake, backup_path
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)

    def test_dry_run_does_not_write_ddb(self):
        result, fake, _ = self._run()
        self.assertEqual(result["status"], "dry_run")
        # Legacy key must still exist
        self.assertIn(LEGACY_KEY, fake._items)
        # UUID item must still be the original stub (no tcu_code)
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertNotIn("tcu_code", uuid_py)

    def test_dry_run_does_not_write_backup_file(self):
        _, _, backup_path = self._run()
        self.assertFalse(os.path.exists(backup_path))

    def test_dry_run_returns_merged_item(self):
        result, _, _ = self._run()
        self.assertIn("merged_item", result)
        self.assertEqual(result["merged_item"]["tcu_code"], LEGACY_TCU_CODE)
        self.assertEqual(result["merged_item"]["cognito_sub"], UUID_SUB)


# ---------------------------------------------------------------------------
# Migration: pre-condition failures
# ---------------------------------------------------------------------------

class TestMigrationPreConditions(unittest.TestCase):

    def _run_migration(self, items):
        fake = FakeDdb(items)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        os.unlink(backup_path)
        try:
            return run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)

    def test_error_when_legacy_tcu_code_unexpected(self):
        """If legacy record has a different tcu_code, migration refuses."""
        modified = {**LEGACY_ITEM_PY, "tcu_code": "9999"}
        items = {LEGACY_KEY: _ddb(modified), UUID_KEY: _ddb(UUID_STUB_PY), CODE_KEY: _ddb(CODE_ITEM_PY)}
        result = self._run_migration(items)
        self.assertEqual(result["status"], "error")
        self.assertIn("tcu_code", result["reason"])

    def test_error_when_uuid_stub_already_has_tcu_code(self):
        """UUID item already has a tcu_code — not a plain stub."""
        stub_with_code = {**UUID_STUB_PY, "tcu_code": "LF1234"}
        items = {LEGACY_KEY: _ddb(LEGACY_ITEM_PY), UUID_KEY: _ddb(stub_with_code), CODE_KEY: _ddb(CODE_ITEM_PY)}
        result = self._run_migration(items)
        self.assertEqual(result["status"], "error")
        self.assertIn("tcu_code", result["reason"])

    def test_error_when_code1001_cognito_sub_already_uuid(self):
        """code#1001.cognito_sub already corrected — refuse to re-run."""
        code_corrected = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB}
        items = {LEGACY_KEY: _ddb(LEGACY_ITEM_PY), UUID_KEY: _ddb(UUID_STUB_PY), CODE_KEY: _ddb(code_corrected)}
        result = self._run_migration(items)
        self.assertEqual(result["status"], "error")
        self.assertIn("cognito_sub", result["reason"])

    def test_error_on_field_conflict_between_records(self):
        """Conflicting field values in legacy vs stub cause migration to refuse."""
        legacy_with_role = {**LEGACY_ITEM_PY, "role": "LF"}
        stub_with_role   = {**UUID_STUB_PY,   "role": "TEST"}
        items = {LEGACY_KEY: _ddb(legacy_with_role), UUID_KEY: _ddb(stub_with_role), CODE_KEY: _ddb(CODE_ITEM_PY)}
        result = self._run_migration(items)
        self.assertEqual(result["status"], "error")
        self.assertIn("conflict", result["reason"].lower())

    def test_inconsistent_state_blocked(self):
        """Inconsistent DDB state (legacy gone but UUID not migrated) → error."""
        items = {UUID_KEY: _ddb(UUID_STUB_PY), CODE_KEY: _ddb(CODE_ITEM_PY)}
        result = self._run_migration(items)
        self.assertEqual(result["status"], "error")


# ---------------------------------------------------------------------------
# Migration: successful commit
# ---------------------------------------------------------------------------

class TestMigrationSuccess(unittest.TestCase):

    def _run(self):
        fake = _pre_migration_ddb()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        return result, fake

    def test_status_complete(self):
        result, _ = self._run()
        self.assertEqual(result["status"], "complete")

    def test_legacy_key_deleted(self):
        _, fake = self._run()
        self.assertNotIn(LEGACY_KEY, fake._items)

    def test_uuid_item_has_tcu_code(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(uuid_py["tcu_code"], LEGACY_TCU_CODE)

    def test_uuid_item_has_correct_cognito_sub(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(uuid_py["cognito_sub"], UUID_SUB)

    def test_uuid_item_preserves_assigned_at_epoch(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(int(uuid_py["assigned_at_epoch"]), 1791177470)

    def test_uuid_item_preserves_role_from_stub(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(uuid_py["role"], "TEST")

    def test_uuid_item_preserves_cognito_username(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(uuid_py["cognito_username"], EMAIL)

    def test_uuid_item_has_migration_audit_fields(self):
        _, fake = self._run()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertEqual(uuid_py["migrated_from_key"], LEGACY_KEY)
        self.assertIn("migrated_at_epoch", uuid_py)

    def test_code1001_cognito_sub_updated(self):
        _, fake = self._run()
        code_py = _item_to_py(fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_sub"], UUID_SUB)

    def test_code1001_other_fields_unchanged(self):
        _, fake = self._run()
        code_py = _item_to_py(fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_username"], EMAIL)
        self.assertEqual(int(code_py["assigned_at_epoch"]), 1791177470)

    def test_backup_file_written_before_writes(self):
        """Backup must be created even if post-checks fail — tested via manual backup."""
        fake = _pre_migration_ddb()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            self.assertTrue(os.path.exists(backup_path))
            with open(backup_path) as f:
                backup = json.load(f)
            self.assertIn(LEGACY_KEY, backup["items"])
            self.assertIn(UUID_KEY, backup["items"])
            self.assertIn(CODE_KEY, backup["items"])
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)


# ---------------------------------------------------------------------------
# Migration: transaction cancellation (concurrent modification)
# ---------------------------------------------------------------------------

class TestMigrationTransactionCancellation(unittest.TestCase):

    def _run_with_modified_item(self, key: str, modified_py: dict | None):
        """Run migration with one item pre-modified to trigger a condition failure."""
        base = {
            LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
            UUID_KEY:   _ddb(UUID_STUB_PY),
            CODE_KEY:   _ddb(CODE_ITEM_PY),
        }
        if modified_py is None:
            base.pop(key, None)
        else:
            base[key] = _ddb(modified_py)
        fake = FakeDdb(base)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        return result

    def test_transaction_cancelled_when_uuid_stub_role_changed(self):
        """UUID stub role changed between read and write → condition fails."""
        modified_stub = {**UUID_STUB_PY, "role": "LF"}  # role no longer "TEST"
        result = self._run_with_modified_item(UUID_KEY, modified_stub)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(UUID_KEY in item for item in result["failed_items"]))

    def test_transaction_cancelled_when_uuid_stub_acquired_tcu_code(self):
        """UUID stub gained a tcu_code between read and transaction → condition fails."""
        stub_with_code = {**UUID_STUB_PY, "tcu_code": "LF9999"}
        # This is caught in pre-checks, not transaction — verify pre-check blocks it
        base = {
            LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
            UUID_KEY:   _ddb(stub_with_code),
            CODE_KEY:   _ddb(CODE_ITEM_PY),
        }
        fake = FakeDdb(base)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        self.assertEqual(result["status"], "error")
        self.assertIn("tcu_code", result["reason"])

    def test_transaction_cancelled_when_legacy_tcu_code_changed(self):
        """Legacy record tcu_code changed concurrently → delete condition fails."""
        modified_legacy = {**LEGACY_ITEM_PY, "tcu_code": "9999"}
        # Pre-check catches this before transaction
        result = self._run_with_modified_item(LEGACY_KEY, modified_legacy)
        self.assertEqual(result["status"], "error")

    def test_transaction_cancelled_when_code1001_already_migrated(self):
        """code#1001.cognito_sub already updated → update condition fails."""
        code_already_migrated = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB}
        # Pre-check catches this
        result = self._run_with_modified_item(CODE_KEY, code_already_migrated)
        self.assertEqual(result["status"], "error")

    def test_transaction_condition_on_uuid_role_via_fake_concurrent_write(self):
        """Simulate: items are fine at read time, but UUID role changes before the transaction
        executes (the classic TOCTOU window). The transaction ConditionExpression catches it."""
        fake = _pre_migration_ddb()
        original_transact = fake.transact_write_items

        call_count = [0]

        def transact_with_concurrent_mutation(TransactItems):
            call_count[0] += 1
            # Simulate a concurrent write that changes UUID stub role just before the tx
            fake._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "role": "LF"})
            return original_transact(TransactItems=TransactItems)

        fake.transact_write_items = transact_with_concurrent_mutation

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(call_count[0], 1)


# ---------------------------------------------------------------------------
# Migration: idempotency
# ---------------------------------------------------------------------------

class TestMigrationIdempotency(unittest.TestCase):

    def test_already_migrated_returns_complete(self):
        fake = _post_migration_ddb()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        os.unlink(backup_path)
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        self.assertEqual(result["status"], "already_complete")
        self.assertTrue(result.get("idempotent"))

    def test_already_migrated_does_not_write_ddb(self):
        fake = _post_migration_ddb()
        pre_state = copy.deepcopy(fake._items)
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        os.unlink(backup_path)
        try:
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        self.assertEqual(fake._items, pre_state)

    def test_repeated_execution_idempotent(self):
        """Running migration twice: first succeeds, second detects already-complete."""
        fake = _pre_migration_ddb()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            r1 = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            r2 = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        self.assertEqual(r1["status"], "complete")
        self.assertEqual(r2["status"], "already_complete")

    def test_crash_after_transaction_recovers_on_rerun(self):
        """Simulate: transaction committed, script crashed before post-checks.
        Re-running detects migrated state and returns already_complete."""
        # Manually set up post-migration DDB state
        fake = _post_migration_ddb()
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        os.unlink(backup_path)
        try:
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        # Must detect the existing migrated state without re-writing
        self.assertEqual(result["status"], "already_complete")
        self.assertTrue(result.get("idempotent"))


# ---------------------------------------------------------------------------
# Rollback: successful
# ---------------------------------------------------------------------------

class TestRollbackSuccess(unittest.TestCase):

    def _run_migration_and_rollback(self):
        """Run migration to completion, then rollback."""
        fake = _pre_migration_ddb()

        # Migration
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            backup_path = f.name
        try:
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)
        return result, fake

    def test_rollback_status_rolled_back(self):
        result, _ = self._run_migration_and_rollback()
        self.assertEqual(result["status"], "rolled_back")

    def test_rollback_restores_legacy_key(self):
        _, fake = self._run_migration_and_rollback()
        self.assertIn(LEGACY_KEY, fake._items)
        legacy_py = _item_to_py(fake._items[LEGACY_KEY])
        self.assertEqual(legacy_py["tcu_code"], LEGACY_TCU_CODE)
        self.assertEqual(legacy_py["cognito_username"], EMAIL)

    def test_rollback_restores_uuid_stub(self):
        _, fake = self._run_migration_and_rollback()
        uuid_py = _item_to_py(fake._items[UUID_KEY])
        self.assertNotIn("tcu_code", uuid_py)
        self.assertNotIn("migrated_from_key", uuid_py)
        self.assertEqual(uuid_py["role"], "TEST")

    def test_rollback_restores_code1001_cognito_sub(self):
        _, fake = self._run_migration_and_rollback()
        code_py = _item_to_py(fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_sub"], LEGACY_COGNITO_SUB)

    def test_rollback_preserves_code1001_other_fields(self):
        _, fake = self._run_migration_and_rollback()
        code_py = _item_to_py(fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_username"], EMAIL)
        self.assertEqual(int(code_py["assigned_at_epoch"]), 1791177470)


# ---------------------------------------------------------------------------
# Rollback: safety conditions
# ---------------------------------------------------------------------------

class TestRollbackSafetyConditions(unittest.TestCase):

    def _make_backup(self):
        with tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w"
        ) as f:
            backup = {
                "backup_created_at_epoch": EPOCH,
                "items": {
                    LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
                    UUID_KEY:   _ddb(UUID_STUB_PY),
                    CODE_KEY:   _ddb(CODE_ITEM_PY),
                },
            }
            json.dump(backup, f, default=str)
            return f.name

    def test_rollback_error_when_backup_missing(self):
        fake = _post_migration_ddb()
        result = run_rollback(fake, "screenlake-tcu-codes", "/tmp/nonexistent_backup.json")
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["reason"].lower())

    def test_rollback_cancelled_when_uuid_item_lacks_migration_fields(self):
        """UUID item has tcu_code but no migrated_from_key — not our migration."""
        merged_no_audit = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        del merged_no_audit["migrated_from_key"]
        items = {
            UUID_KEY:  _ddb(merged_no_audit),
            CODE_KEY:  _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
        }
        fake = FakeDdb(items)
        backup_path = self._make_backup()
        try:
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            os.unlink(backup_path)
        self.assertEqual(result["status"], "error")
        self.assertIn("migrated_from_key", result["reason"])

    def test_rollback_cancelled_when_legacy_key_already_exists(self):
        """Legacy key was already restored (or migration never ran) — tx condition fails."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        items = {
            LEGACY_KEY: _ddb(LEGACY_ITEM_PY),  # already exists
            UUID_KEY:   _ddb(merged),
            CODE_KEY:   _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
        }
        fake = FakeDdb(items)
        backup_path = self._make_backup()
        try:
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            os.unlink(backup_path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(LEGACY_KEY in item for item in result["failed_items"]))

    def test_rollback_cancelled_when_code1001_already_reverted(self):
        """code#1001.cognito_sub is already the legacy value — another process reverted it."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        items = {
            UUID_KEY:  _ddb(merged),
            CODE_KEY:  _ddb(CODE_ITEM_PY),  # cognito_sub = "mythubanh" (already reverted)
        }
        fake = FakeDdb(items)
        backup_path = self._make_backup()
        try:
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            os.unlink(backup_path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(CODE_KEY in item for item in result["failed_items"]))

    def test_rollback_cancelled_when_uuid_item_tcu_code_changed(self):
        """UUID item was modified after migration (tcu_code ≠ 1001) — refuse rollback."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        merged["tcu_code"] = "RS0006"  # changed after migration
        items = {
            UUID_KEY:  _ddb(merged),
            CODE_KEY:  _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
        }
        fake = FakeDdb(items)
        backup_path = self._make_backup()
        try:
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            os.unlink(backup_path)
        self.assertIn(result["status"], ("error", "cancelled"))


# ---------------------------------------------------------------------------
# DDB type conversion round-trip
# ---------------------------------------------------------------------------

class TestTypeConversions(unittest.TestCase):

    def test_round_trip_string(self):
        item = {"id": "test#key", "name": "alice"}
        self.assertEqual(_item_to_py(_item_to_ddb(item)), item)

    def test_round_trip_number(self):
        item = {"id": "x", "epoch": Decimal("1791177470")}
        result = _item_to_py(_item_to_ddb(item))
        self.assertEqual(result["epoch"], Decimal("1791177470"))

    def test_round_trip_bool(self):
        item = {"id": "x", "flag": True}
        self.assertEqual(_item_to_py(_item_to_ddb(item)), item)


if __name__ == "__main__":
    unittest.main()
