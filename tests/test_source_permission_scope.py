"""Scoped reader unit contracts; not evidence of a deployed SQL server."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import sqlserver_demo as adapter  # noqa: E402


class Cursor:
    """Reject any accidental unrelated-object query and inject local faults."""

    def __init__(self, fault=None):
        self.fault = fault
        self.rows = []
        self.statements = []

    def execute(self, query, args=()):
        self.statements.append((query, args))
        if "sys.objects" in query or "sys.columns" in query:
            raise AssertionError("Unrelated objects must not be enumerated")
        if query.startswith("SELECT IS_MEMBER"):
            if "IS_SRVROLEMEMBER" in query:
                count = query.count("IS_MEMBER(") + query.count(
                    "IS_SRVROLEMEMBER("
                )
                self.rows = [
                    tuple([int(self.fault == "broad")] + [0] * (count - 1))
                ]
            else:
                self.rows = [(0 if self.fault == "role" else 1,)]
        elif "fn_my_permissions(NULL" in query:
            self.rows = [("CONTROL",)] if self.fault == "database" else []
        elif "N'SCHEMA'" in query:
            self.rows = [(1 if self.fault == "schema" else 0,)]
        elif "HAS_PERMS_BY_NAME" in query:
            self.rows = [(0 if self.fault == "select" else 1,)]
        elif "fn_my_permissions" in query:
            self.rows = [("SELECT",)]
            if self.fault in {
                "INSERT",
                "UPDATE",
                "DELETE",
                "ALTER",
                "CONTROL",
                "TAKE OWNERSHIP",
            }:
                self.rows.append((self.fault,))
            if self.fault == "unknown_fixed":
                self.rows = []
        elif query.startswith("SELECT TOP (0)"):
            if self.fault == "probe":
                raise RuntimeError("synthetic read refused")
            self.rows = []
        else:
            raise AssertionError(query)

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows


class ScopedReaderTests(unittest.TestCase):
    def test_unrelated_objects_are_not_probed(self):
        cursor = Cursor()
        self.assertTrue(adapter.permissions_safe(cursor, role="source"))
        self.assertEqual(
            sum(q.startswith("SELECT TOP (0)") for q, _ in cursor.statements),
            7,
        )
        self.assertTrue(
            all(q.startswith("SELECT ") for q, _ in cursor.statements)
        )

    def test_required_boundary_fails_closed(self):
        for fault in (
            "broad",
            "database",
            "schema",
            "select",
            "role",
            "unknown_fixed",
            "INSERT",
            "UPDATE",
            "DELETE",
            "ALTER",
            "CONTROL",
            "TAKE OWNERSHIP",
        ):
            with self.subTest(fault=fault):
                self.assertFalse(
                    adapter.permissions_safe(Cursor(fault), role="source")
                )

    def test_real_read_failure_is_not_ignored(self):
        with self.assertRaises(RuntimeError):
            adapter.permissions_safe(Cursor("probe"), role="source")


if __name__ == "__main__":
    unittest.main()
