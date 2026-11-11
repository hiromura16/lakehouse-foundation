"""Observe SQL checkpoint contracts with a fake SELECT-only connection.

No SQL Server connection, grant, DDL or checkpoint write occurs in these
unit tests. Actual driver permissions and SQL execution need DBA acceptance.
"""

import contextlib
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "checkpoint_subject", ROOT / "scripts/sqlserver_batch.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TABLES = {
    name: {}
    for name in (
        "erp_products",
        "erp_work_orders",
        "erp_sales_orders",
        "erp_shipments",
        "mes_events",
        "mes_downtime",
    )
}
VALUES = {
    "SOURCE_SQLSERVER_CONTROL_USER": "fixture_control",
    "SOURCE_SQLSERVER_CONTROL_PASSWORD": "fixture-control-secret",
}


def fixture(rows):
    """Record every statement and keep the connection transaction untouched."""
    cursor = mock.Mock()
    cursor.fetchmany.return_value = rows
    handle = mock.Mock()
    handle.cursor.return_value = cursor
    adapter = SimpleNamespace(
        TABLES=TABLES,
        validate_settings=mock.Mock(return_value=VALUES),
        ownership=mock.Mock(),
        permissions_safe=mock.Mock(return_value=True),
    )

    @contextlib.contextmanager
    def connect(values, *, role):
        """Check the narrow identity selection without using a real driver."""
        if values != VALUES or role != "control":
            raise AssertionError("Unexpected diagnostic connection scope.")
        yield handle

    adapter.connection = connect
    return adapter, cursor, handle


class CheckpointObservationTests(unittest.TestCase):
    """Read only complete, owned and consistently identified positions."""

    def test_absent_control_credentials_does_not_open_a_connection(self):
        adapter, cursor, _ = fixture([])
        result = MODULE.inspect_checkpoints({}, adapter=adapter)
        self.assertFalse(result["configured"])
        self.assertFalse(result["baseline_complete"])
        self.assertEqual(result["watermarks"], {})
        adapter.validate_settings.assert_not_called()
        cursor.execute.assert_not_called()

    def test_complete_baseline_preserves_seven_digit_clock_and_uses_select(
        self,
    ):
        rows = [
            (name, "2026-04-01T12:00:00.1234567", "K1", "a" * 64)
            for name in TABLES
        ]
        adapter, cursor, handle = fixture(rows)
        result = MODULE.inspect_checkpoints(VALUES, adapter=adapter)
        self.assertTrue(result["baseline_complete"])
        self.assertEqual(
            result["watermarks"]["erp_products"]["timestamp"],
            "2026-04-01T12:00:00.1234567Z",
        )
        sql = cursor.execute.call_args.args[0]
        self.assertTrue(sql.startswith("SELECT TOP (7)"))
        self.assertNotIn("UPDLOCK", sql)
        self.assertNotIn("HOLDLOCK", sql)
        handle.commit.assert_not_called()
        adapter.ownership.assert_called_once_with(
            cursor, VALUES, role="control"
        )
        adapter.permissions_safe.assert_called_once_with(
            cursor, role="control"
        )
        self.assertNotIn("fixture-control-secret", str(result))

    def test_empty_owned_control_table_is_not_an_initialized_baseline(self):
        adapter, _, handle = fixture([])
        result = MODULE.inspect_checkpoints(VALUES, adapter=adapter)
        self.assertTrue(result["configured"])
        self.assertFalse(result["baseline_complete"])
        handle.commit.assert_not_called()

    def test_partial_unknown_duplicate_or_private_driver_failure_is_redacted(
        self,
    ):
        row = ("erp_products", None, None, "a" * 64)
        for rows in ([row], [row, row], [("unrelated", None, None, "a" * 64)]):
            adapter, _, handle = fixture(rows)
            with self.assertRaises(MODULE.BatchError):
                MODULE.inspect_checkpoints(VALUES, adapter=adapter)
            handle.commit.assert_not_called()
        adapter, cursor, handle = fixture([])
        cursor.execute.side_effect = RuntimeError("fixture-control-secret")
        with self.assertRaises(MODULE.BatchError) as caught:
            MODULE.inspect_checkpoints(VALUES, adapter=adapter)
        self.assertNotIn("fixture-control-secret", str(caught.exception))
        handle.commit.assert_not_called()

    def test_unsafe_permissions_refuse_before_control_row_query(self):
        adapter, cursor, handle = fixture([])
        adapter.permissions_safe.return_value = False
        with self.assertRaises(MODULE.BatchError):
            MODULE.inspect_checkpoints(VALUES, adapter=adapter)
        cursor.execute.assert_not_called()
        handle.commit.assert_not_called()

    def test_partial_control_credentials_refuse_before_connection(self):
        """Never silently substitute the read-only SOURCE credentials."""
        adapter, cursor, _ = fixture([])
        with self.assertRaises(MODULE.BatchError):
            MODULE.inspect_checkpoints(
                {"SOURCE_SQLSERVER_CONTROL_USER": "fixture_control"},
                adapter=adapter,
            )
        adapter.validate_settings.assert_not_called()
        cursor.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
