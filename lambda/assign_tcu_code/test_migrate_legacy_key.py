"""Tests for migrate_legacy_key.py.

Covers:
  - build_snapshot_condition: field pinning, absent-field guards, name aliasing
  - Field analysis: conflict detection, merge correctness
  - State detection: not_started, complete, inconsistent
  - Dry-run: no DDB writes, no backup file
  - Pre-condition failures: unexpected values, inconsistent state, field conflicts
  - Successful migration: all fields preserved, audit fields present, backup written
  - Transaction cancellation: each item fails its condition
    - Concurrent change to a migration-modified field
    - Concurrent change to an UNRELATED field (the new coverage)
    - New field added to a record between backup and transaction
  - Backup safety: atomic write, no-overwrite, verify-on-reuse, diverged-state detection
  - Idempotency: already-migrated state, repeated execution, crash-then-recover
  - Rollback success: all three items restored to exact original content
  - Rollback safety: each item fails its condition
    - Legitimate post-migration tcu_code update (migrate_researcher ran)
    - Legitimate post-migration role change
    - Other field changed on UUID item after migration
    - Legacy key already exists
    - code#1001 claimed after migration
    - code#1001 retired after migration
  - Type conversion round-trips
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
    CODE1001_GUARDED_ABSENT,
    CODE_KEY,
    EMAIL,
    LEGACY_COGNITO_SUB,
    LEGACY_KEY,
    LEGACY_KEY_GUARDED_ABSENT,
    LEGACY_TCU_CODE,
    UUID_KEY,
    UUID_MIGRATED_GUARDED_ABSENT,
    UUID_STUB_GUARDED_ABSENT,
    UUID_SUB,
    MigrationState,
    build_expected_migrated_item,
    build_merged_item,
    build_snapshot_condition,
    detect_conflicts,
    detect_state,
    run_migration,
    run_rollback,
    _item_to_ddb,
    _item_to_py,
    _py_to_ddb,
    _verify_backup_matches_live,
    _write_backup_atomic,
)

# ---------------------------------------------------------------------------
# Canonical test fixtures (plain Python, not DDB wire format)
# ---------------------------------------------------------------------------

LEGACY_ITEM_PY = {
    "id":               LEGACY_KEY,
    "tcu_code":         LEGACY_TCU_CODE,
    "cognito_username": EMAIL,
    "assigned_at_epoch": Decimal("1791177470"),
}

UUID_STUB_PY = {
    "id":   UUID_KEY,
    "role": "TEST",
}

CODE_ITEM_PY = {
    "id":               CODE_KEY,
    "cognito_sub":      LEGACY_COGNITO_SUB,
    "cognito_username": EMAIL,
    "assigned_at_epoch": Decimal("1791177470"),
}

EPOCH = 1800000000


def _ddb(py_item: dict) -> dict:
    return _item_to_ddb(py_item)


def _py(ddb_item: dict) -> dict:
    return _item_to_py(ddb_item)


# ---------------------------------------------------------------------------
# FakeDdb — in-memory DynamoDB low-level client
# ---------------------------------------------------------------------------

class FakeDdb:
    """Minimal in-memory DynamoDB low-level client for transaction testing.

    Supports get_item and transact_write_items (Put, Delete, Update with SET).
    Evaluates ConditionExpressions including:
      - attribute_exists(#alias)
      - attribute_not_exists(#alias)
      - #alias = :placeholder
      - Depth-aware AND
      - (A OR B) groups
    ExpressionAttributeNames are resolved before comparisons.
    """

    def __init__(self, items: dict[str, dict]):
        self._items = {k: copy.deepcopy(v) for k, v in items.items() if v is not None}

    def get_item(self, TableName, Key):
        pk = _py(Key)["id"]
        item = self._items.get(pk)
        return {"Item": copy.deepcopy(item)} if item else {}

    def transact_write_items(self, TransactItems):
        # Phase 1: evaluate all conditions; collect failures
        results = []
        for op in TransactItems:
            kind = next(iter(op))
            spec = op[kind]
            if kind == "Put":
                pk = _py(spec["Item"])["id"]
            else:  # Delete or Update
                pk = _py(spec["Key"])["id"]
            existing = self._items.get(pk)
            cond      = spec.get("ConditionExpression", "")
            expr_vals = {
                k: _py({k: v})[k]
                for k, v in spec.get("ExpressionAttributeValues", {}).items()
            }
            expr_names = spec.get("ExpressionAttributeNames", {})
            passed = self._eval_condition(cond, existing, expr_vals, expr_names)
            results.append({"Code": "None"} if passed else {"Code": "ConditionalCheckFailed"})

        if any(r["Code"] == "ConditionalCheckFailed" for r in results):
            err_response = {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": results,
            }
            from botocore.exceptions import ClientError
            raise ClientError(err_response, "TransactWriteItems")

        # Phase 2: apply all mutations
        for op in TransactItems:
            kind = next(iter(op))
            spec = op[kind]
            expr_vals = {
                k: _py({k: v})[k]
                for k, v in spec.get("ExpressionAttributeValues", {}).items()
            }
            expr_names = spec.get("ExpressionAttributeNames", {})

            if kind == "Put":
                item = _py(spec["Item"])
                self._items[item["id"]] = copy.deepcopy(spec["Item"])

            elif kind == "Delete":
                pk = _py(spec["Key"])["id"]
                self._items.pop(pk, None)

            elif kind == "Update":
                pk = _py(spec["Key"])["id"]
                update_expr = spec.get("UpdateExpression", "")
                if update_expr.upper().startswith("SET "):
                    for assignment in update_expr[4:].split(","):
                        assignment = assignment.strip()
                        name_part, val_part = [s.strip() for s in assignment.split("=", 1)]
                        # Resolve ExpressionAttributeNames alias
                        field = expr_names.get(name_part, name_part)
                        if pk not in self._items:
                            self._items[pk] = {"id": spec["Key"]["id"]}
                        self._items[pk][field] = _py_to_ddb(expr_vals[val_part])

    # --- ConditionExpression evaluator ---

    def _eval_condition(
        self,
        cond: str,
        existing: dict | None,
        expr_vals: dict,
        expr_names: dict,
    ) -> bool:
        if not cond:
            return True
        existing_py = _item_to_py(existing) if existing else {}

        def resolve(name: str) -> str:
            return expr_names.get(name, name)

        def eval_atom(atom: str) -> bool:
            atom = atom.strip()
            if "attribute_not_exists(" in atom:
                field = atom.split("(", 1)[1].rstrip(")")
                return resolve(field) not in existing_py
            if "attribute_exists(" in atom:
                field = atom.split("(", 1)[1].rstrip(")")
                return resolve(field) in existing_py
            if "=" in atom and "attribute" not in atom:
                lhs, rhs = [s.strip() for s in atom.split("=", 1)]
                lhs = resolve(lhs)
                if rhs in expr_vals:
                    return existing_py.get(lhs) == expr_vals[rhs]
            return True

        def eval_expr(expr: str) -> bool:
            expr = expr.strip()
            # Strip outer parens if fully wrapped
            if expr.startswith("(") and expr.endswith(")"):
                depth = 0
                fully = True
                for i, ch in enumerate(expr):
                    if ch == "(": depth += 1
                    elif ch == ")": depth -= 1
                    if depth == 0 and i < len(expr) - 1:
                        fully = False
                        break
                if fully:
                    expr = expr[1:-1].strip()
            # Depth-aware AND split
            depth, parts, current = 0, [], []
            i = 0
            while i < len(expr):
                ch = expr[i]
                if ch == "(":
                    depth += 1; current.append(ch)
                elif ch == ")":
                    depth -= 1; current.append(ch)
                elif depth == 0 and expr[i:i+4].upper() == " AND":
                    parts.append("".join(current).strip()); current = []; i += 4; continue
                else:
                    current.append(ch)
                i += 1
            if current:
                parts.append("".join(current).strip())
            # Evaluate each AND clause (which may itself be "(A OR B)")
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
# Helpers
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
    return FakeDdb({
        UUID_KEY:  _ddb(merged),
        CODE_KEY:  _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
    })


def _run_migration(fake, dry_run=False, backup_path=None):
    """Run migration with a temp backup file unless one is provided."""
    cleanup = backup_path is None
    if backup_path is None:
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(backup_path)
    try:
        return run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=dry_run)
    finally:
        if cleanup and os.path.exists(backup_path):
            os.unlink(backup_path)


def _run_rollback(fake, backup_path):
    return run_rollback(fake, "screenlake-tcu-codes", backup_path)


# ---------------------------------------------------------------------------
# build_snapshot_condition tests
# ---------------------------------------------------------------------------

class TestBuildSnapshotCondition(unittest.TestCase):

    def test_pins_every_non_id_field(self):
        item = {"id": "x", "role": "TEST", "tcu_code": "1001"}
        cond, names, vals = build_snapshot_condition(item, [])
        self.assertIn("#f_role = :v_role", cond)
        self.assertIn("#f_tcu_code = :v_tcu_code", cond)
        self.assertNotIn("id", cond)

    def test_absent_field_guards_use_attribute_not_exists(self):
        item = {"id": "x"}
        cond, names, vals = build_snapshot_condition(item, ["tcu_code", "email"])
        self.assertIn("attribute_not_exists(#a_tcu_code)", cond)
        self.assertIn("attribute_not_exists(#a_email)", cond)

    def test_all_field_names_aliased_with_hash(self):
        """Every name in ExpressionAttributeNames must start with '#'."""
        item = {"id": "x", "role": "TEST"}
        _, names, _ = build_snapshot_condition(item, ["tcu_code"])
        for alias in names:
            self.assertTrue(alias.startswith("#"), f"Name alias {alias!r} lacks # prefix")

    def test_values_are_ddb_wire_format(self):
        item = {"id": "x", "role": "TEST", "epoch": Decimal("123")}
        _, _, vals = build_snapshot_condition(item, [])
        for key, val in vals.items():
            self.assertIsInstance(val, dict, f"Value at {key!r} is not DDB wire format")
            self.assertTrue(len(val) == 1, f"Value at {key!r} has multiple type keys")

    def test_condition_joined_by_and(self):
        item = {"id": "x", "a": "1", "b": "2"}
        cond, _, _ = build_snapshot_condition(item, ["c"])
        self.assertIn(" AND ", cond)

    def test_no_field_overlap_between_present_and_absent_aliases(self):
        """#f_ and #a_ prefixes must not collide."""
        item = {"id": "x", "role": "TEST"}
        _, names, _ = build_snapshot_condition(item, ["role"])  # role in both
        present_aliases = {k for k in names if k.startswith("#f_")}
        absent_aliases  = {k for k in names if k.startswith("#a_")}
        self.assertTrue(present_aliases.isdisjoint(absent_aliases))


# ---------------------------------------------------------------------------
# FakeDdb snapshot condition evaluation
# ---------------------------------------------------------------------------

class TestFakeDbSnapshotCondition(unittest.TestCase):
    """Verify FakeDdb evaluates the conditions generated by build_snapshot_condition."""

    def _make_ddb(self, item_py):
        return FakeDdb({"test#key": _ddb(item_py)})

    def test_passes_when_item_matches_snapshot(self):
        item = {"id": "test#key", "role": "TEST", "epoch": Decimal("100")}
        fake = self._make_ddb(item)
        cond, names, vals = build_snapshot_condition(item, [])
        existing = fake._items.get("test#key")
        result = fake._eval_condition(cond, existing,
            {k: _py({k: v})[k] for k, v in vals.items()}, names)
        self.assertTrue(result)

    def test_fails_when_one_field_differs(self):
        item = {"id": "test#key", "role": "TEST"}
        fake = self._make_ddb({**item, "role": "LF"})  # live value differs
        cond, names, vals = build_snapshot_condition(item, [])
        existing = fake._items.get("test#key")
        result = fake._eval_condition(cond, existing,
            {k: _py({k: v})[k] for k, v in vals.items()}, names)
        self.assertFalse(result)

    def test_fails_when_guarded_absent_field_is_present(self):
        item = {"id": "test#key", "role": "TEST"}
        fake = self._make_ddb({**item, "tcu_code": "LF1234"})  # unwanted field present
        cond, names, vals = build_snapshot_condition(item, ["tcu_code"])
        existing = fake._items.get("test#key")
        result = fake._eval_condition(cond, existing,
            {k: _py({k: v})[k] for k, v in vals.items()}, names)
        self.assertFalse(result)

    def test_passes_when_guarded_absent_field_is_truly_absent(self):
        item = {"id": "test#key", "role": "TEST"}
        fake = self._make_ddb(item)
        cond, names, vals = build_snapshot_condition(item, ["tcu_code"])
        existing = fake._items.get("test#key")
        result = fake._eval_condition(cond, existing,
            {k: _py({k: v})[k] for k, v in vals.items()}, names)
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Field analysis tests
# ---------------------------------------------------------------------------

class TestFieldAnalysis(unittest.TestCase):

    def test_no_conflicts_for_actual_records(self):
        self.assertEqual(detect_conflicts(LEGACY_ITEM_PY, UUID_STUB_PY), [])

    def test_conflict_detected_when_shared_field_differs(self):
        conflicts = detect_conflicts(
            {**LEGACY_ITEM_PY, "role": "LF"},
            {**UUID_STUB_PY,   "role": "TEST"},
        )
        self.assertEqual(len(conflicts), 1)
        self.assertIn("role", conflicts[0])

    def test_no_conflict_when_shared_field_identical(self):
        conflicts = detect_conflicts(
            {**LEGACY_ITEM_PY, "role": "TEST"},
            {**UUID_STUB_PY,   "role": "TEST"},
        )
        self.assertEqual(conflicts, [])

    def test_id_never_flagged(self):
        self.assertEqual(
            detect_conflicts({"id": LEGACY_KEY, "foo": "x"}, {"id": UUID_KEY, "foo": "x"}),
            []
        )

    def test_merged_item_contains_all_legacy_fields(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        for field, value in LEGACY_ITEM_PY.items():
            if field == "id": continue
            self.assertEqual(merged[field], value)

    def test_merged_item_preserves_stub_only_fields(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertEqual(merged["role"], "TEST")

    def test_merged_item_has_correct_id_and_cognito_sub(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertEqual(merged["id"], UUID_KEY)
        self.assertEqual(merged["cognito_sub"], UUID_SUB)

    def test_merged_item_has_audit_fields(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        self.assertEqual(merged["migrated_from_key"], LEGACY_KEY)
        self.assertEqual(merged["migrated_at_epoch"], EPOCH)

    def test_legacy_wins_on_conflict(self):
        merged = build_merged_item(
            {**LEGACY_ITEM_PY, "role": "LF"},
            {**UUID_STUB_PY,   "role": "TEST"},
            EPOCH,
        )
        self.assertEqual(merged["role"], "LF")

    def test_expected_migrated_item_excludes_migrated_at_epoch(self):
        em = build_expected_migrated_item(LEGACY_ITEM_PY, UUID_STUB_PY)
        self.assertNotIn("migrated_at_epoch", em)

    def test_expected_migrated_item_includes_migrated_from_key(self):
        em = build_expected_migrated_item(LEGACY_ITEM_PY, UUID_STUB_PY)
        self.assertEqual(em["migrated_from_key"], LEGACY_KEY)


# ---------------------------------------------------------------------------
# State detection
# ---------------------------------------------------------------------------

class TestDetectState(unittest.TestCase):

    def test_not_started(self):
        s, _ = detect_state(LEGACY_ITEM_PY, UUID_STUB_PY, CODE_ITEM_PY)
        self.assertEqual(s, MigrationState.NOT_STARTED)

    def test_complete(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        s, _ = detect_state(None, merged, {**CODE_ITEM_PY, "cognito_sub": UUID_SUB})
        self.assertEqual(s, MigrationState.COMPLETE)

    def test_inconsistent_legacy_gone_uuid_not_migrated(self):
        s, _ = detect_state(None, UUID_STUB_PY, CODE_ITEM_PY)
        self.assertEqual(s, MigrationState.INCONSISTENT)

    def test_inconsistent_both_exist_and_uuid_has_code(self):
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        s, _ = detect_state(LEGACY_ITEM_PY, merged, CODE_ITEM_PY)
        self.assertEqual(s, MigrationState.INCONSISTENT)

    def test_inconsistent_missing_code_item(self):
        s, _ = detect_state(LEGACY_ITEM_PY, UUID_STUB_PY, None)
        self.assertEqual(s, MigrationState.INCONSISTENT)


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

class TestBackupHelpers(unittest.TestCase):

    def _make_live_items(self):
        return (
            _ddb(LEGACY_ITEM_PY),
            _ddb(UUID_STUB_PY),
            _ddb(CODE_ITEM_PY),
        )

    def test_verify_backup_no_discrepancies_when_matching(self):
        legacy_raw, uuid_raw, code_raw = self._make_live_items()
        backup = {"items": {LEGACY_KEY: legacy_raw, UUID_KEY: uuid_raw, CODE_KEY: code_raw}}
        result = _verify_backup_matches_live(backup, legacy_raw, uuid_raw, code_raw)
        self.assertEqual(result, [])

    def test_verify_backup_detects_changed_field(self):
        legacy_raw, uuid_raw, code_raw = self._make_live_items()
        modified_legacy = _ddb({**LEGACY_ITEM_PY, "tcu_code": "9999"})
        backup = {"items": {LEGACY_KEY: legacy_raw, UUID_KEY: uuid_raw, CODE_KEY: code_raw}}
        result = _verify_backup_matches_live(backup, modified_legacy, uuid_raw, code_raw)
        self.assertTrue(any("tcu_code" in d for d in result))

    def test_verify_backup_detects_added_field(self):
        legacy_raw, uuid_raw, code_raw = self._make_live_items()
        extra_legacy = _ddb({**LEGACY_ITEM_PY, "new_field": "surprise"})
        backup = {"items": {LEGACY_KEY: legacy_raw, UUID_KEY: uuid_raw, CODE_KEY: code_raw}}
        result = _verify_backup_matches_live(backup, extra_legacy, uuid_raw, code_raw)
        self.assertTrue(any("new_field" in d for d in result))

    def test_verify_backup_detects_missing_item(self):
        legacy_raw, uuid_raw, code_raw = self._make_live_items()
        backup = {"items": {LEGACY_KEY: legacy_raw, UUID_KEY: uuid_raw, CODE_KEY: code_raw}}
        result = _verify_backup_matches_live(backup, None, uuid_raw, code_raw)  # legacy missing live
        self.assertTrue(any(LEGACY_KEY in d for d in result))

    def test_atomic_write_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_backup.json")
            _write_backup_atomic({"key": "value"}, path)
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                data = json.load(f)
            self.assertEqual(data["key"], "value")

    def test_atomic_write_no_partial_file_on_error(self):
        """If os.rename fails, neither the target path nor a tmp file should remain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "test_backup.json")
            with patch("migrate_legacy_key.os.rename", side_effect=OSError("simulated disk full")):
                with self.assertRaises(OSError):
                    _write_backup_atomic({"key": "value"}, path)
            # Target must not exist
            self.assertFalse(os.path.exists(path))
            # No leftover tmp files either
            self.assertEqual(os.listdir(tmpdir), [])

    def test_backup_not_overwritten_when_already_exists(self):
        """Re-running migration with existing backup file must not overwrite it."""
        fake = _pre_migration_ddb()
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(backup_path)   # let the first migration run create it fresh
        try:
            # First run writes backup
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            with open(backup_path) as f:
                first_content = f.read()
            # Second run on already-migrated state reuses backup without overwrite
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            with open(backup_path) as f:
                second_content = f.read()
            self.assertEqual(first_content, second_content)
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)

    def test_backup_reuse_blocked_when_live_state_diverged(self):
        """If existing backup disagrees with live DDB, migration must refuse."""
        fake = _pre_migration_ddb()
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            # Write a stale backup (legacy tcu_code = "9999" but live is "1001")
            stale_backup = {
                "backup_created_at_epoch": EPOCH,
                "items": {
                    LEGACY_KEY: _ddb({**LEGACY_ITEM_PY, "tcu_code": "9999"}),
                    UUID_KEY:   _ddb(UUID_STUB_PY),
                    CODE_KEY:   _ddb(CODE_ITEM_PY),
                },
            }
            with open(backup_path, "w") as f:
                json.dump(stale_backup, f)
            result = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            self.assertEqual(result["status"], "error")
            self.assertIn("differs from existing backup", result["reason"].lower())
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)

    def test_dry_run_does_not_create_backup_file(self):
        fake = _pre_migration_ddb()
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(backup_path)
        try:
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=True)
            self.assertFalse(os.path.exists(backup_path))
        finally:
            if os.path.exists(backup_path):
                os.unlink(backup_path)


# ---------------------------------------------------------------------------
# Migration: pre-condition failures
# ---------------------------------------------------------------------------

class TestMigrationPreConditions(unittest.TestCase):

    def test_error_when_legacy_tcu_code_unexpected(self):
        fake = _pre_migration_ddb(**{LEGACY_KEY: _ddb({**LEGACY_ITEM_PY, "tcu_code": "9999"})})
        self.assertEqual(_run_migration(fake)["status"], "error")

    def test_error_when_uuid_stub_already_has_tcu_code(self):
        fake = _pre_migration_ddb(**{UUID_KEY: _ddb({**UUID_STUB_PY, "tcu_code": "LF1234"})})
        result = _run_migration(fake)
        self.assertEqual(result["status"], "error")
        self.assertIn("tcu_code", result["reason"])

    def test_error_when_code1001_cognito_sub_already_uuid(self):
        fake = _pre_migration_ddb(**{CODE_KEY: _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB})})
        self.assertEqual(_run_migration(fake)["status"], "error")

    def test_error_on_field_conflict(self):
        fake = _pre_migration_ddb(
            **{
                LEGACY_KEY: _ddb({**LEGACY_ITEM_PY, "role": "LF"}),
                UUID_KEY:   _ddb({**UUID_STUB_PY,   "role": "TEST"}),
            }
        )
        result = _run_migration(fake)
        self.assertEqual(result["status"], "error")
        self.assertIn("conflict", result["reason"].lower())

    def test_inconsistent_state_blocked(self):
        fake = FakeDdb({UUID_KEY: _ddb(UUID_STUB_PY), CODE_KEY: _ddb(CODE_ITEM_PY)})
        self.assertEqual(_run_migration(fake)["status"], "error")


# ---------------------------------------------------------------------------
# Migration: dry-run
# ---------------------------------------------------------------------------

class TestMigrationDryRun(unittest.TestCase):

    def test_no_ddb_writes(self):
        fake = _pre_migration_ddb()
        _run_migration(fake, dry_run=True)
        self.assertIn(LEGACY_KEY, fake._items)
        self.assertNotIn("tcu_code", _py(fake._items[UUID_KEY]))

    def test_returns_merged_item(self):
        result = _run_migration(_pre_migration_ddb(), dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["merged_item"]["tcu_code"], LEGACY_TCU_CODE)
        self.assertEqual(result["merged_item"]["cognito_sub"], UUID_SUB)


# ---------------------------------------------------------------------------
# Migration: successful commit
# ---------------------------------------------------------------------------

class TestMigrationSuccess(unittest.TestCase):

    def setUp(self):
        self.fake = _pre_migration_ddb()
        self._backup = tempfile.mkstemp(suffix=".json")
        os.close(self._backup[0])
        os.unlink(self._backup[1])
        self.backup_path = self._backup[1]
        self.result = run_migration(
            self.fake, "screenlake-tcu-codes", self.backup_path, dry_run=False
        )

    def tearDown(self):
        if os.path.exists(self.backup_path):
            os.unlink(self.backup_path)

    def test_status_complete(self):
        self.assertEqual(self.result["status"], "complete")

    def test_legacy_key_deleted(self):
        self.assertNotIn(LEGACY_KEY, self.fake._items)

    def test_uuid_item_has_tcu_code(self):
        self.assertEqual(_py(self.fake._items[UUID_KEY])["tcu_code"], LEGACY_TCU_CODE)

    def test_uuid_item_has_correct_cognito_sub(self):
        self.assertEqual(_py(self.fake._items[UUID_KEY])["cognito_sub"], UUID_SUB)

    def test_uuid_item_preserves_assigned_at_epoch(self):
        self.assertEqual(int(_py(self.fake._items[UUID_KEY])["assigned_at_epoch"]), 1791177470)

    def test_uuid_item_preserves_role_from_stub(self):
        self.assertEqual(_py(self.fake._items[UUID_KEY])["role"], "TEST")

    def test_uuid_item_preserves_cognito_username(self):
        self.assertEqual(_py(self.fake._items[UUID_KEY])["cognito_username"], EMAIL)

    def test_uuid_item_has_migration_audit_fields(self):
        uuid_py = _py(self.fake._items[UUID_KEY])
        self.assertEqual(uuid_py["migrated_from_key"], LEGACY_KEY)
        self.assertIn("migrated_at_epoch", uuid_py)

    def test_code1001_cognito_sub_updated_to_uuid(self):
        self.assertEqual(_py(self.fake._items[CODE_KEY])["cognito_sub"], UUID_SUB)

    def test_code1001_other_fields_unchanged(self):
        code_py = _py(self.fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_username"], EMAIL)
        self.assertEqual(int(code_py["assigned_at_epoch"]), 1791177470)

    def test_backup_file_created(self):
        self.assertTrue(os.path.exists(self.backup_path))
        with open(self.backup_path) as f:
            backup = json.load(f)
        self.assertIn(LEGACY_KEY, backup["items"])
        self.assertIn(UUID_KEY,   backup["items"])
        self.assertIn(CODE_KEY,   backup["items"])


# ---------------------------------------------------------------------------
# Migration: transaction cancellation
# ---------------------------------------------------------------------------

class TestMigrationTransactionCancellation(unittest.TestCase):
    """Each test modifies the live DDB just before or at transaction time
    to verify the corresponding ConditionExpression rejects the write."""

    def _run_with_concurrent_write(self, modify_fn):
        """Run migration, injecting modify_fn to fire before transact_write_items."""
        fake = _pre_migration_ddb()
        original_transact = fake.transact_write_items

        def patched_transact(TransactItems):
            modify_fn(fake)
            return original_transact(TransactItems=TransactItems)

        fake.transact_write_items = patched_transact
        return _run_migration(fake)

    # --- UUID stub concurrent modifications ---

    def test_cancelled_when_uuid_role_changed(self):
        def modify(f): f._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "role": "LF"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_uuid_gains_tcu_code(self):
        """Concurrent assignment adds tcu_code — absent-field guard fires."""
        def modify(f): f._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "tcu_code": "LF9999"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_uuid_gains_email(self):
        """Concurrent RS claim adds email — absent-field guard fires."""
        def modify(f): f._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "email": EMAIL})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_uuid_gains_cognito_username(self):
        """Any assignment adds cognito_username — absent-field guard fires."""
        def modify(f): f._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "cognito_username": EMAIL})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_uuid_gains_from_pre_reservation(self):
        def modify(f): f._items[UUID_KEY] = _ddb({**UUID_STUB_PY, "from_pre_reservation": True})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    # --- Legacy key concurrent modifications ---

    def test_cancelled_when_legacy_tcu_code_changed(self):
        def modify(f): f._items[LEGACY_KEY] = _ddb({**LEGACY_ITEM_PY, "tcu_code": "9999"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_legacy_cognito_username_changed(self):
        def modify(f): f._items[LEGACY_KEY] = _ddb({**LEGACY_ITEM_PY, "cognito_username": "other@test.com"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_legacy_assigned_at_epoch_changed(self):
        def modify(f): f._items[LEGACY_KEY] = _ddb({**LEGACY_ITEM_PY, "assigned_at_epoch": Decimal("9999999999")})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_legacy_gains_role(self):
        """Absent-field guard: legacy item should not have a role field."""
        def modify(f): f._items[LEGACY_KEY] = _ddb({**LEGACY_ITEM_PY, "role": "RS"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_legacy_gains_deleted_flag(self):
        """retire_researcher would add deleted=True — guard catches it."""
        def modify(f): f._items[LEGACY_KEY] = _ddb({**LEGACY_ITEM_PY, "deleted": True})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    # --- code#1001 concurrent modifications ---

    def test_cancelled_when_code1001_cognito_sub_already_migrated(self):
        def modify(f): f._items[CODE_KEY] = _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_code1001_cognito_username_changed(self):
        def modify(f): f._items[CODE_KEY] = _ddb({**CODE_ITEM_PY, "cognito_username": "other@test.com"})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_code1001_claimed(self):
        """Absent-field guard: code should not be claimed during migration."""
        def modify(f): f._items[CODE_KEY] = _ddb({**CODE_ITEM_PY, "claimed": True, "claimed_by_sub": UUID_SUB})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")

    def test_cancelled_when_code1001_retired(self):
        def modify(f): f._items[CODE_KEY] = _ddb({**CODE_ITEM_PY, "retired": True})
        self.assertEqual(self._run_with_concurrent_write(modify)["status"], "cancelled")


# ---------------------------------------------------------------------------
# Migration: idempotency and crash recovery
# ---------------------------------------------------------------------------

class TestMigrationIdempotency(unittest.TestCase):

    def test_already_migrated_returns_complete(self):
        result = _run_migration(_post_migration_ddb())
        self.assertEqual(result["status"], "already_complete")
        self.assertTrue(result.get("idempotent"))

    def test_already_migrated_no_ddb_writes(self):
        fake = _post_migration_ddb()
        pre_state = copy.deepcopy(fake._items)
        _run_migration(fake)
        self.assertEqual(fake._items, pre_state)

    def test_repeated_execution(self):
        fake = _pre_migration_ddb()
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd); os.unlink(backup_path)
        try:
            r1 = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            r2 = run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
        finally:
            if os.path.exists(backup_path): os.unlink(backup_path)
        self.assertEqual(r1["status"], "complete")
        self.assertEqual(r2["status"], "already_complete")

    def test_crash_after_transaction_recovers(self):
        """Transaction committed, script crashed before post-checks → re-run succeeds."""
        result = _run_migration(_post_migration_ddb())
        self.assertEqual(result["status"], "already_complete")


# ---------------------------------------------------------------------------
# Rollback: successful
# ---------------------------------------------------------------------------

class TestRollbackSuccess(unittest.TestCase):

    def _migrate_and_rollback(self):
        fake = _pre_migration_ddb()
        fd, backup_path = tempfile.mkstemp(suffix=".json")
        os.close(fd); os.unlink(backup_path)
        try:
            run_migration(fake, "screenlake-tcu-codes", backup_path, dry_run=False)
            result = run_rollback(fake, "screenlake-tcu-codes", backup_path)
        finally:
            if os.path.exists(backup_path): os.unlink(backup_path)
        return result, fake

    def test_status(self):
        result, _ = self._migrate_and_rollback()
        self.assertEqual(result["status"], "rolled_back")

    def test_legacy_key_restored(self):
        _, fake = self._migrate_and_rollback()
        legacy_py = _py(fake._items[LEGACY_KEY])
        self.assertEqual(legacy_py["tcu_code"], LEGACY_TCU_CODE)
        self.assertEqual(legacy_py["cognito_username"], EMAIL)
        self.assertEqual(int(legacy_py["assigned_at_epoch"]), 1791177470)

    def test_uuid_stub_restored(self):
        _, fake = self._migrate_and_rollback()
        uuid_py = _py(fake._items[UUID_KEY])
        self.assertNotIn("tcu_code", uuid_py)
        self.assertNotIn("migrated_from_key", uuid_py)
        self.assertEqual(uuid_py["role"], "TEST")

    def test_code1001_cognito_sub_restored(self):
        _, fake = self._migrate_and_rollback()
        self.assertEqual(_py(fake._items[CODE_KEY])["cognito_sub"], LEGACY_COGNITO_SUB)

    def test_code1001_other_fields_preserved(self):
        _, fake = self._migrate_and_rollback()
        code_py = _py(fake._items[CODE_KEY])
        self.assertEqual(code_py["cognito_username"], EMAIL)
        self.assertEqual(int(code_py["assigned_at_epoch"]), 1791177470)


# ---------------------------------------------------------------------------
# Rollback: safety conditions
# ---------------------------------------------------------------------------

class TestRollbackSafetyConditions(unittest.TestCase):

    def _make_backup(self):
        """Write a standard pre-migration backup to a temp file."""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        backup = {
            "backup_created_at_epoch": EPOCH,
            "items": {
                LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
                UUID_KEY:   _ddb(UUID_STUB_PY),
                CODE_KEY:   _ddb(CODE_ITEM_PY),
            },
        }
        with open(path, "w") as f:
            json.dump(backup, f, default=str)
        return path

    def _post_migration_items(self, **overrides):
        """Base post-migration DDB state, optionally modified."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        items = {
            UUID_KEY:  _ddb(merged),
            CODE_KEY:  _ddb({**CODE_ITEM_PY, "cognito_sub": UUID_SUB}),
        }
        items.update(overrides)
        return items

    def test_error_when_backup_missing(self):
        result = run_rollback(FakeDdb({}), "screenlake-tcu-codes", "/tmp/nonexistent.json")
        self.assertEqual(result["status"], "error")

    def test_error_when_uuid_missing_migrated_from_key(self):
        """UUID item exists but lacks migration provenance field."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        del merged["migrated_from_key"]
        items = self._post_migration_items(**{UUID_KEY: _ddb(merged)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], "error")
        self.assertIn("migrated_from_key", result["reason"])

    def test_rollback_refused_when_tcu_code_updated_after_migration(self):
        """migrate_researcher changed tcu_code to RS0006 — rollback must refuse."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        merged["tcu_code"] = "RS0006"
        items = self._post_migration_items(**{UUID_KEY: _ddb(merged)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertIn(result["status"], ("cancelled", "error"))

    def test_rollback_refused_when_role_changed_after_migration(self):
        """A subsequent write updated role from TEST to RS."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        merged["role"] = "RS"
        items = self._post_migration_items(**{UUID_KEY: _ddb(merged)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertIn(result["status"], ("cancelled", "error"))

    def test_rollback_refused_when_cognito_username_changed_on_uuid(self):
        """An unrelated field on the UUID item was changed."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        merged["cognito_username"] = "changed@example.com"
        items = self._post_migration_items(**{UUID_KEY: _ddb(merged)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertIn(result["status"], ("cancelled", "error"))

    def test_rollback_refused_when_account_retired_after_migration(self):
        """retire_researcher added deleted=True — rollback must refuse."""
        merged = build_merged_item(LEGACY_ITEM_PY, UUID_STUB_PY, EPOCH)
        merged["deleted"] = True
        merged["deleted_at_epoch"] = 1800000001
        items = self._post_migration_items(**{UUID_KEY: _ddb(merged)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(UUID_KEY in item for item in result["failed_items"]))

    def test_rollback_refused_when_legacy_key_already_exists(self):
        """Legacy key already present — rollback Put condition fails."""
        items = {
            **self._post_migration_items(),
            LEGACY_KEY: _ddb(LEGACY_ITEM_PY),
        }
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(LEGACY_KEY in item for item in result["failed_items"]))

    def test_rollback_refused_when_code1001_claimed(self):
        """code#1001 was claimed after migration — rollback must refuse."""
        code_claimed = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB,
                        "claimed": True, "claimed_by_sub": UUID_SUB,
                        "claimed_at_epoch": Decimal("1800000001")}
        items = self._post_migration_items(**{CODE_KEY: _ddb(code_claimed)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(CODE_KEY in item for item in result["failed_items"]))

    def test_rollback_refused_when_code1001_retired(self):
        """code#1001 was retired after migration — rollback must refuse."""
        code_retired = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB,
                        "retired": True, "retired_at_epoch": Decimal("1800000001")}
        items = self._post_migration_items(**{CODE_KEY: _ddb(code_retired)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue(any(CODE_KEY in item for item in result["failed_items"]))

    def test_rollback_refused_when_code1001_cognito_username_changed(self):
        """An unrelated field on code#1001 changed after migration."""
        code_changed = {**CODE_ITEM_PY, "cognito_sub": UUID_SUB,
                        "cognito_username": "other@example.com"}
        items = self._post_migration_items(**{CODE_KEY: _ddb(code_changed)})
        fake = FakeDdb(items)
        path = self._make_backup()
        try:
            result = _run_rollback(fake, path)
        finally:
            os.unlink(path)
        self.assertIn(result["status"], ("cancelled", "error"))


# ---------------------------------------------------------------------------
# Type conversion round-trips
# ---------------------------------------------------------------------------

class TestTypeConversions(unittest.TestCase):

    def test_round_trip_string(self):
        item = {"id": "test#key", "name": "alice"}
        self.assertEqual(_item_to_py(_item_to_ddb(item)), item)

    def test_round_trip_number(self):
        item = {"id": "x", "epoch": Decimal("1791177470")}
        self.assertEqual(_item_to_py(_item_to_ddb(item)), item)

    def test_round_trip_bool(self):
        item = {"id": "x", "flag": True}
        self.assertEqual(_item_to_py(_item_to_ddb(item)), item)


if __name__ == "__main__":
    unittest.main()
