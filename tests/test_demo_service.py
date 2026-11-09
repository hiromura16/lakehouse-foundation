"""Check synthetic GUI sources without network, Docker or a real database."""

import fcntl
import importlib.util
import json
import tempfile
import unittest
import zlib
from hashlib import sha256
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "demo_service_subject", ROOT / "infra/demo/service.py"
)
SERVICE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVICE)
RUN_ID = "012345abcdef"


class DemoSourceTests(unittest.TestCase):
    """Verify incremental source contracts and source-only write boundaries."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="demo-service-test-")
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "landing"

    def generate(self, **changes):
        """Create a small bounded fixture with independently chosen options."""
        options = {"run_id": RUN_ID, "orders": 2, "scenario": "clean"}
        options.update(changes)
        return SERVICE.generate_batch(self.output, **options)

    def test_batch_publishes_six_sources_and_hashes_without_network(self):
        with (
            mock.patch("socket.create_connection", side_effect=AssertionError),
            mock.patch("subprocess.run", side_effect=AssertionError),
        ):
            result = self.generate(images=True)
        self.assertEqual(len(result["sources"]), 6)
        self.assertEqual(len(result["images"]), 1)
        self.assertIsNone(result["sql_schema"])
        for item in result["sources"] + result["images"]:
            path = self.output / item["path"]
            digest = sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest, item["sha256"])
            self.assertEqual(
                path.with_name(path.name + ".ready").read_text().strip(),
                digest,
            )
            self.assertIn("__" + RUN_ID + "_0001.", path.name)
        manifest = json.loads((self.output / result["manifest"]).read_text())
        self.assertEqual(manifest, result)
        self.assertEqual(list(self.output.rglob("*.part")), [])

    def test_distinct_batches_preserve_references_and_revision_order(self):
        first = SERVICE.namespaced_data(RUN_ID, 1, 42, 2, 7, "edge")
        second = SERVICE.namespaced_data(RUN_ID, 2, 42, 2, 7, "edge")
        first_ids = {row["event_id"] for row in first["mes_events"]}
        second_ids = {row["event_id"] for row in second["mes_events"]}
        self.assertFalse(first_ids & second_ids)
        products = {row["product_id"] for row in first["erp_products"]}
        self.assertIn(first["erp_work_orders"][0]["product_id"], products)
        work_orders = {
            row["work_order_id"] for row in first["erp_work_orders"]
        }
        self.assertIn(first["mes_events"][0]["work_order_id"], work_orders)
        event_id = first["mes_events"][0]["event_id"]
        versions = [
            row for row in first["mes_events"] if row["event_id"] == event_id
        ]
        self.assertGreater(
            max(row["updated_at"] for row in versions),
            min(row["updated_at"] for row in versions),
        )
        self.assertNotEqual(
            first["mes_events"][0]["updated_at"],
            second["mes_events"][0]["updated_at"],
        )
        self.generate(batch=1)
        self.generate(batch=2)
        self.assertEqual(len(list(self.output.rglob("*.ready"))), 12)

    def test_retry_cannot_overwrite_or_repeat_a_sql_write(self):
        self.generate()
        with mock.patch.object(SERVICE.GENERATOR, "write_sql") as writer:
            with self.assertRaisesRegex(ValueError, "already been"):
                self.generate()
            writer.assert_not_called()

    def test_anomalies_apply_after_namespacing_and_record_diagnostics(self):
        result = self.generate(
            anomalies=["missing_required"], anomaly_rate=100
        )
        source = next(
            item for item in result["sources"] if item["table"] == "mes_events"
        )
        rows = [
            json.loads(line)
            for line in (self.output / source["path"]).read_text().splitlines()
        ]
        self.assertTrue(all(row["event_id"] == "" for row in rows))
        self.assertTrue(all(RUN_ID in row["work_order_id"] for row in rows))
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["anomalies"], ["missing_required"])
        self.assertFalse(diagnostics["intentional_file_faults"])

    def test_wrong_marker_reports_actual_payload_and_intentional_fault(self):
        result = self.generate(
            anomalies=["wrong_checksum"], allow_file_faults=True
        )
        fault = result["diagnostics"]["file_faults"][0]
        path = self.output / fault["path"]
        actual = sha256(path.read_bytes()).hexdigest()
        self.assertNotEqual(
            path.with_name(path.name + ".ready").read_text().strip(), actual
        )
        source = next(
            item for item in result["sources"] if item["path"] == fault["path"]
        )
        self.assertEqual(source["sha256"], actual)
        self.assertTrue(result["diagnostics"]["intentional_file_faults"])

    def test_file_faults_do_not_modify_sql_rows_and_require_consent(self):
        with self.assertRaisesRegex(ValueError, "consent"):
            self.generate(anomalies=["extra_column"])
        self.assertFalse(self.output.exists())
        values = {
            "DEMO_SQLSERVER_HOST": "demo.invalid",
            "DEMO_SQLSERVER_PORT": "1433",
            "DEMO_SQLSERVER_DATABASE": "ManufacturingDemo",
            "DEMO_SQLSERVER_USER": "demo_writer",
            "DEMO_SQLSERVER_PASSWORD": "test-placeholder",
        }
        with mock.patch.object(
            SERVICE.GENERATOR, "write_sql", return_value="lh_demo_synthetic"
        ) as writer:
            self.generate(
                destination="both",
                allow_sql_write=True,
                settings=values,
                anomalies=["extra_column"],
                allow_file_faults=True,
            )
        expected = SERVICE.namespaced_data(RUN_ID, 1, 42, 2, 7, "clean")
        self.assertEqual(writer.call_args.args[0], expected)

    def test_sql_requires_consent_and_refuses_source_and_system_databases(
        self,
    ):
        with mock.patch.object(SERVICE.GENERATOR, "write_sql") as writer:
            with self.assertRaisesRegex(ValueError, "consent"):
                self.generate(destination="sqlserver")
            for database in ["master", "CompanySource"]:
                with self.subTest(database=database):
                    with self.assertRaises(ValueError):
                        self.generate(
                            destination="sqlserver",
                            allow_sql_write=True,
                            settings={
                                "DEMO_SQLSERVER_HOST": "demo.invalid",
                                "DEMO_SQLSERVER_PORT": "1433",
                                "DEMO_SQLSERVER_DATABASE": database,
                                "DEMO_SQLSERVER_USER": "demo_writer",
                                "DEMO_SQLSERVER_PASSWORD": "test-placeholder",
                                "SOURCE_SQLSERVER_DATABASE": "CompanySource",
                            },
                        )
            writer.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_sql_uses_validated_settings_and_returns_no_credentials(self):
        values = {
            "DEMO_SQLSERVER_HOST": "demo.invalid",
            "DEMO_SQLSERVER_PORT": "1433",
            "DEMO_SQLSERVER_DATABASE": "ManufacturingDemo",
            "DEMO_SQLSERVER_USER": "demo_writer",
            "DEMO_SQLSERVER_PASSWORD": "test-placeholder",
            "DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE": (
                "certificate.example.invalid"
            ),
        }
        with mock.patch.object(
            SERVICE.GENERATOR, "write_sql", return_value="lh_demo_synthetic"
        ) as writer:
            result = self.generate(
                destination="sqlserver",
                allow_sql_write=True,
                settings=values,
            )
        writer.assert_called_once()
        self.assertEqual(writer.call_args.args[1], values)
        self.assertEqual(result["sql_schema"], "lh_demo_synthetic")
        self.assertEqual(list(self.output.rglob("*.ready")), [])
        self.assertNotIn("test-placeholder", json.dumps(result))
        self.assertNotIn("demo.invalid", json.dumps(result))

    def test_symlink_and_concurrent_writer_are_refused(self):
        self.output.mkdir()
        (self.output / "images").symlink_to(
            self.output, target_is_directory=True
        )
        with self.assertRaisesRegex(ValueError, "real directories"):
            self.generate(images=True)
        (self.output / "images").unlink()
        with (self.output / ".demo-gui.lock").open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Another source"):
                self.generate()

    def test_png_is_valid_and_options_are_bounded(self):
        payload = SERVICE.synthetic_png(42)
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        length = int.from_bytes(payload[33:37], "big")
        self.assertEqual(payload[37:41], b"IDAT")
        start = 41
        stop = start + length
        pixels = zlib.decompress(payload[start:stop])
        self.assertEqual(len(pixels), 96 * (1 + 96 * 3))
        for options in [
            {"run_id": "bad/path"},
            {"batch": 0},
            {"orders": 1001},
            {"seed": True},
            {"days": 367},
            {"scenario": "unknown"},
        ]:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.generate(**options)


if __name__ == "__main__":
    unittest.main()
