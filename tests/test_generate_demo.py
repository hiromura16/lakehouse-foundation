"""Test source generation with temporary files and a recording SQL driver."""

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "source_generator", ROOT / "scripts/generate-demo.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


class RecordingCursor:
    """Record SQL and supply metadata/count query results."""

    def __init__(self, existing_schema=False):
        self.statements = []
        self.inserted = 0
        self.last = ""
        self.existing_schema = existing_schema

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        self.last = statement

    def executemany(self, statement, records):
        self.statements.append(statement)
        self.inserted = len(records)

    def fetchone(self):
        if self.last == "SELECT DB_NAME()":
            return ("ManufacturingDemo",)
        if self.last == "SELECT SCHEMA_ID(%s)":
            return (1 if self.existing_schema else None,)
        return (self.inserted,)


class RecordingConnection:
    """Track transaction disposition without opening a database connection."""

    def __init__(self, existing_schema=False):
        self.recorder = RecordingCursor(existing_schema)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.recorder

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class SourceGeneratorTests(unittest.TestCase):
    """Check source realism, repeatability and SQL write boundaries."""

    def test_clean_references_and_multiple_children(self):
        data = GENERATOR.build_data(scenario="clean")
        products = {row["product_id"] for row in data["erp_products"]}
        work_orders = {row["work_order_id"] for row in data["erp_work_orders"]}
        sales_orders = {
            row["sales_order_id"] for row in data["erp_sales_orders"]
        }
        self.assertEqual(len(data["mes_events"]), 40)
        self.assertEqual(len(data["erp_shipments"]), 40)
        for row in data["erp_work_orders"] + data["erp_sales_orders"]:
            self.assertIn(row["product_id"], products)
        for row in data["mes_events"] + data["mes_downtime"]:
            self.assertIn(row["work_order_id"], work_orders)
        for row in data["erp_shipments"]:
            self.assertIn(row["sales_order_id"], sales_orders)

    def test_seed_is_reproducible_and_affects_quantities(self):
        first = GENERATOR.encoded_sources(GENERATOR.build_data(seed=42))
        same = GENERATOR.encoded_sources(GENERATOR.build_data(seed=42))
        changed = GENERATOR.encoded_sources(GENERATOR.build_data(seed=43))
        self.assertEqual(first, same)
        self.assertNotEqual(first, changed)

    def test_latest_revision_and_late_arrival_are_distinct(self):
        data = GENERATOR.build_data()
        original = data["mes_events"][0]
        versions = [
            row
            for row in data["mes_events"]
            if row["event_id"] == original["event_id"]
        ]
        self.assertEqual(len(versions), 3)
        latest = max(versions, key=lambda row: row["updated_at"])
        self.assertEqual(
            int(latest["good_qty"]), int(original["good_qty"]) + 3
        )
        late = next(
            row for row in data["mes_events"] if row["event_id"] == "EV-LATE"
        )
        self.assertGreater(
            datetime.fromisoformat(late["updated_at"].replace("Z", "+00:00")),
            datetime.fromisoformat(late["event_time"].replace("Z", "+00:00")),
        )

    def test_markers_and_overwrite_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "sources"
            GENERATOR.main(["--output", str(output)])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertNotIn("checks", manifest)
            for source in manifest["sources"]:
                path = output / source["path"]
                digest = sha256(path.read_bytes()).hexdigest()
                self.assertEqual(digest, source["sha256"])
                self.assertEqual(
                    path.with_name(path.name + ".ready").read_text().strip(),
                    digest,
                )
            with self.assertRaises(ValueError):
                GENERATOR.main(["--output", str(output)])
            GENERATOR.main(["--output", str(output), "--force"])

    def test_sql_requires_explicit_write_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(GENERATOR, "write_sql") as writer:
                with self.assertRaises(ValueError):
                    GENERATOR.main(
                        ["--output", directory, "--destination", "sqlserver"]
                    )
                writer.assert_not_called()

    def test_system_and_company_source_databases_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private.env"
            for database in ["master", "MODEL", "CompanyProduction"]:
                path.write_text(
                    "DEMO_SQLSERVER_HOST=demo.invalid\n"
                    "DEMO_SQLSERVER_PORT=1433\n"
                    f"DEMO_SQLSERVER_DATABASE={database}\n"
                    "DEMO_SQLSERVER_USER=demo_writer\n"
                    "DEMO_SQLSERVER_PASSWORD=synthetic-test-password\n"
                    "SOURCE_SQLSERVER_DATABASE=CompanyProduction\n"
                )
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    GENERATOR.private_demo_settings(path)

    def test_sql_is_insert_only_in_a_new_schema_with_required_encryption(self):
        connection = RecordingConnection()
        driver = mock.Mock()
        driver.connect.return_value = connection
        values = {
            "DEMO_SQLSERVER_HOST": "demo.invalid",
            "DEMO_SQLSERVER_PORT": "1433",
            "DEMO_SQLSERVER_DATABASE": "ManufacturingDemo",
            "DEMO_SQLSERVER_USER": "demo_writer",
            "DEMO_SQLSERVER_PASSWORD": "synthetic-test-password",
        }
        with mock.patch.dict(sys.modules, {"pymssql": driver}):
            schema = GENERATOR.write_sql(GENERATOR.build_data(), values)
        self.assertRegex(schema, r"^lh_demo_[0-9a-f]{16}$")
        self.assertTrue(connection.committed)
        self.assertFalse(connection.rolled_back)
        statements = connection.recorder.statements
        self.assertEqual(
            sum(s.startswith("CREATE TABLE") for s in statements), 6
        )
        self.assertEqual(
            sum(s.startswith("INSERT INTO") for s in statements), 6
        )
        self.assertFalse(
            any(
                s.startswith(("UPDATE", "DELETE", "DROP", "CREATE DATABASE"))
                for s in statements
            )
        )
        self.assertEqual(
            driver.connect.call_args.kwargs["encryption"], "require"
        )

    def test_existing_schema_is_never_reused(self):
        connection = RecordingConnection(existing_schema=True)
        driver = mock.Mock()
        driver.connect.return_value = connection
        values = {
            "DEMO_SQLSERVER_HOST": "demo.invalid",
            "DEMO_SQLSERVER_PORT": "1433",
            "DEMO_SQLSERVER_DATABASE": "ManufacturingDemo",
            "DEMO_SQLSERVER_USER": "demo_writer",
            "DEMO_SQLSERVER_PASSWORD": "synthetic-test-password",
        }
        with mock.patch.dict(sys.modules, {"pymssql": driver}):
            with self.assertRaises(RuntimeError):
                GENERATOR.write_sql(GENERATOR.build_data(), values)
        self.assertTrue(connection.rolled_back)
        self.assertFalse(
            any(
                statement.startswith("CREATE")
                for statement in connection.recorder.statements
            )
        )


if __name__ == "__main__":
    unittest.main()
