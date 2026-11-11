"""Check that AI consumers receive bounded, snapshot-pinned facts only."""

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "ai_context_subject", ROOT / "scripts/ai_context.py"
)
CONTEXT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONTEXT)


class ContextTests(unittest.TestCase):
    """Verify factual references and publication boundaries without a model."""

    def test_export_selects_current_main_even_after_rollback(self):
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.side_effect = [[(12345,)], [("F001", 72)]]
        cursor.description = [("factory_id",), ("good_qty",)]
        result = CONTEXT.collect(connection, "production", 20)
        statements = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertIn("$refs", statements[0])
        self.assertIn("WHERE name = 'main' LIMIT 1", statements[0])
        self.assertNotIn("committed_at", statements[0])
        self.assertIn("FOR VERSION AS OF 12345", statements[1])
        self.assertTrue(statements[1].endswith("LIMIT 20"))
        self.assertEqual(result["snapshot_id"], "12345")
        self.assertEqual(
            result["rows"], [{"factory_id": "F001", "good_qty": 72}]
        )
        self.assertFalse(result["complete_dataset"])
        cursor.close.assert_called_once()

    def test_export_hashes_describe_checkout_not_snapshot_generation(self):
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.side_effect = [[(12345,)], []]
        cursor.description = [("factory_id",)]
        connect = mock.Mock(return_value=connection)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "medallion").mkdir()
            reference = b"-- Export checkout reference, not build evidence.\n"
            (root / "medallion/03_gold.sql").write_bytes(reference)
            with mock.patch.object(CONTEXT, "ROOT", root):
                result = CONTEXT.export_context(
                    {"BIND_IP": "localhost", "TRINO_PORT": "8080"},
                    ["production"],
                    connect=connect,
                )
        self.assertNotIn("transformation_sha256", result)
        self.assertEqual(
            result["exporter_reference_sql_sha256"],
            hashlib.sha256(reference).hexdigest(),
        )
        self.assertEqual(
            result["exporter_code_sha256"],
            hashlib.sha256(Path(CONTEXT.__file__).read_bytes()).hexdigest(),
        )
        self.assertEqual(result["datasets"][0]["snapshot_id"], "12345")
        self.assertTrue(
            any("do not identify" in note for note in result["notes"])
        )

    def test_unknown_datasets_and_limits_never_reach_sql(self):
        connection = mock.Mock()
        for name, limit in [("production; DELETE", 1), ("sales", 501)]:
            with self.assertRaises(ValueError):
                CONTEXT.collect(connection, name, limit)
        connection.cursor.assert_not_called()

    def test_finite_origins_pin_their_own_gold_snapshots(self):
        for origin, schema in (
            ("landing", "gold"),
            ("batch", "sql_gold"),
            ("cdc", "sqlcdc_gold"),
        ):
            with self.subTest(origin=origin):
                connection = mock.Mock()
                cursor = connection.cursor.return_value
                cursor.fetchall.side_effect = [[(12345,)], []]
                cursor.description = [("factory_id",)]
                result = CONTEXT.collect(
                    connection, "production", 10, origin=origin
                )
                sql = [call.args[0] for call in cursor.execute.call_args_list]
                self.assertIn("iceberg." + schema + '."', sql[0])
                self.assertIn("FOR VERSION AS OF 12345", sql[1])
                self.assertEqual(
                    result["table"],
                    "iceberg." + schema + ".factory_daily_production",
                )
                self.assertEqual(result["origin"], origin)

    def test_origin_injection_and_boolean_limits_are_rejected_before_sql(self):
        connection = mock.Mock()
        for origin in ("sql_gold", "batch; DELETE", "unknown"):
            with self.assertRaises(ValueError):
                CONTEXT.collect(connection, "production", origin=origin)
        for limit in (True, 0, 501):
            with self.assertRaises(ValueError):
                CONTEXT.collect(connection, "production", limit)
        connection.cursor.assert_not_called()
        connect = mock.Mock()
        with self.assertRaises(ValueError):
            CONTEXT.export_context(
                {}, ["production"], connect=connect, origin="arbitrary_schema"
            )
        connect.assert_not_called()

    def test_invalid_snapshots_and_excess_rows_do_not_escape_bounds(self):
        for value in (True, -1, "12345", 2**64):
            connection = mock.Mock()
            connection.cursor.return_value.fetchall.return_value = [(value,)]
            with self.assertRaises(ValueError):
                CONTEXT.collect(connection, "production")
            self.assertEqual(
                connection.cursor.return_value.execute.call_count, 1
            )
        connection = mock.Mock()
        cursor = connection.cursor.return_value
        cursor.fetchall.side_effect = [[(1,)], [("F1",), ("F2",)]]
        cursor.description = [("factory_id",)]
        with self.assertRaises(ValueError):
            CONTEXT.collect(connection, "production", 1)

    def test_private_export_is_new_and_has_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "facts.json"
            CONTEXT.write_private(output, {"context_version": 1})
            self.assertEqual(os.stat(output).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                CONTEXT.write_private(output, {"context_version": 2})

    def test_export_into_the_checkout_is_rejected(self):
        with self.assertRaises(ValueError):
            CONTEXT.write_private(ROOT / "facts.json", {})


if __name__ == "__main__":
    unittest.main()
