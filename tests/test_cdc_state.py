"""Check CDC ordering, replay recovery, deletion, and source isolation."""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import config  # noqa: E402
import consumer  # noqa: E402
import state  # noqa: E402


class CdcStateTests(unittest.TestCase):
    """Exercise durable recovery rather than only generated SQL strings."""

    def setUp(self):
        """Prepare a minimal product mapping and isolated SQLite volume."""
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.sqlite3"
        self.store = state.State(self.path, {"source": "test"})
        self.contract = config.CONTRACTS[0]
        self.mapping = {
            "target_table": "erp_products",
            "source_schema": "dbo",
            "source_table": "erp_products",
            "columns": {name: name for name in self.contract["columns"]},
        }
        self.previous_database = os.environ.get("CDC_SQLSERVER_DATABASE")
        os.environ["CDC_SQLSERVER_DATABASE"] = "lh_cdc_demo_test"

    def tearDown(self):
        """Close durable handles and restore process environment."""
        self.store.connection.close()
        self.temporary.cleanup()
        if self.previous_database is None:
            os.environ.pop("CDC_SQLSERVER_DATABASE", None)
        else:
            os.environ["CDC_SQLSERVER_DATABASE"] = self.previous_database

    def record(self, offset, operation="c", sequence=1, name="Part"):
        """Represent a real SQL Server envelope with ordered LSN components."""
        row = dict(
            zip(
                self.contract["columns"],
                [
                    "P1",
                    name,
                    "FAMILY",
                    "piece",
                    "30",
                    "2026-02-01T08:00:00+00:00",
                ],
                strict=True,
            )
        )
        envelope = {
            "op": operation,
            "before": row if operation in {"u", "d"} else None,
            "after": None if operation == "d" else row,
            "source": {
                "db": "lh_cdc_demo_test",
                "schema": "dbo",
                "table": "erp_products",
                "commit_lsn": "00000001:00000001:0001",
                "change_lsn": f"00000001:00000001:{sequence:04x}",
                "event_serial_no": sequence,
            },
        }
        return {
            "topic": "factory.dbo.erp_products",
            "partition": 0,
            "offset": offset,
            "key": '{"product_id":"P1"}',
            "value": json.dumps(envelope),
            "ingested_at": "2026-02-01T08:00:00+00:00",
        }

    def stage(self, record):
        """Apply one envelope through the production state implementation."""
        self.store.stage(
            record, "erp_products", self.mapping, self.contract["columns"]
        )

    def test_lsn_order_wins_over_delivery_and_updated_at(self):
        """An older replay cannot replace a newer database revision."""
        self.stage(self.record(2, "u", 3, "New"))
        self.stage(self.record(3, "u", 2, "Old"))
        self.assertEqual(
            self.store.rows("erp_products")[0]["product_name"], "New"
        )

    def test_delete_keeps_version_against_old_insert(self):
        """A replayed insert cannot resurrect a newer SQL deletion."""
        self.stage(self.record(0))
        self.stage(self.record(1, "d", 3))
        self.stage(self.record(2, "c", 2))
        self.assertEqual(self.store.rows("erp_products"), [])

    def test_new_insert_after_delete_is_visible(self):
        """A later source reinsert creates a live current-state row."""
        self.stage(self.record(1, "d", 3))
        self.stage(self.record(2, "c", 4, "Reinserted"))
        self.assertEqual(len(self.store.rows("erp_products")), 1)

    def test_snapshot_cannot_replace_streaming_change(self):
        """Streaming LSNs remain newer than delayed snapshot reads."""
        self.stage(self.record(0, "u", 2, "Live"))
        self.stage(self.record(1, "r", 8, "Snapshot"))
        self.assertEqual(
            self.store.rows("erp_products")[0]["product_name"], "Live"
        )

    def test_replayed_event_keeps_first_ingestion_metadata(self):
        """Reprocessing the same version does not change Bronze metadata."""
        record = self.record(0)
        self.stage(record)
        first = self.store.rows("erp_products")
        record["ingested_at"] = "2026-02-02T08:00:00+00:00"
        self.stage(record)
        self.assertEqual(self.store.rows("erp_products"), first)

    def test_staged_change_does_not_advance_completed_offset(self):
        """Durable row staging alone cannot acknowledge Kafka messages."""
        record = self.record(0)
        self.stage(record)
        self.assertEqual(self.store.offset(record["topic"], 0), -1)
        self.store.complete([record])
        self.assertEqual(self.store.offset(record["topic"], 0), 0)

    def test_state_survives_restart_with_delete_marker(self):
        """Restart recovery retains tombstones and completed offsets."""
        record = self.record(0, "d", 3)
        self.stage(record)
        self.store.complete([record])
        self.store.connection.close()
        self.store = state.State(self.path, {"source": "test"})
        self.stage(self.record(1, "c", 2))
        self.assertEqual(self.store.rows("erp_products"), [])
        self.assertEqual(self.store.offset(record["topic"], 0), 0)

    def test_different_source_cannot_reuse_state(self):
        """A changed database identity requires a different durable volume."""
        with self.assertRaises(ValueError):
            state.State(self.path, {"source": "another"})

    def test_invalid_key_or_lsn_is_rejected(self):
        """Invalid business keys and streaming positions stop work."""
        record = self.record(0)
        record["key"] = '{"other":"P1"}'
        with self.assertRaises(ValueError):
            self.stage(record)
        record = self.record(0)
        envelope = json.loads(record["value"])
        envelope["source"]["commit_lsn"] = "bad"
        record["value"] = json.dumps(envelope)
        with self.assertRaises(ValueError):
            self.stage(record)

    def test_raw_retention_precedes_current_and_checkpoint(self):
        """A failed medallion keeps raw data without advancing offsets."""
        record = self.record(0)
        admin = SimpleNamespace(catalog=lambda: None, STATE={"blocked": False})
        observed = []

        def raw(catalog, records):
            """Observe the required raw-first write boundary."""
            observed.append(("raw", len(self.store.rows("erp_products"))))
            return 1

        def fail(admin, store):
            """Fail after state staging but before completed checkpoint."""
            observed.append(("materialize", len(store.rows("erp_products"))))
            raise RuntimeError("simulated SQL failure")

        with (
            patch.object(consumer, "append_raw", raw),
            patch.object(consumer, "materialize", fail),
        ):
            with self.assertRaises(RuntimeError):
                consumer.process(
                    admin,
                    self.store,
                    [record],
                    {"erp_products": self.mapping},
                    {record["topic"]: "erp_products"},
                )
        self.assertEqual(observed, [("raw", 0), ("materialize", 1)])
        self.assertEqual(self.store.offset(record["topic"], 0), -1)
        with (
            patch.object(consumer, "append_raw", return_value=0),
            patch.object(
                consumer, "materialize", return_value={"sql_statements": 14}
            ),
        ):
            consumer.process(
                admin,
                self.store,
                [record],
                {"erp_products": self.mapping},
                {record["topic"]: "erp_products"},
            )
        self.assertEqual(self.store.offset(record["topic"], 0), 0)
        self.assertEqual(len(self.store.rows("erp_products")), 1)

    def test_uncertain_cancel_persists_blocked_state(self):
        """Restart cannot silently clear an unknown running SQL query."""
        record = self.record(0)
        admin = SimpleNamespace(catalog=lambda: None, STATE={"blocked": True})
        with (
            patch.object(consumer, "append_raw", return_value=1),
            patch.object(
                consumer, "materialize", side_effect=RuntimeError("unknown")
            ),
        ):
            with self.assertRaises(RuntimeError):
                consumer.process(
                    admin,
                    self.store,
                    [record],
                    {"erp_products": self.mapping},
                    {record["topic"]: "erp_products"},
                )
        self.assertEqual(self.store.get("blocked"), "true")
        self.assertEqual(self.store.offset(record["topic"], 0), -1)

    def test_tombstone_raw_is_not_discarded(self):
        """Null Kafka values remain distinct raw records with exact offsets."""
        record = self.record(4)
        record["value"] = None
        rows = consumer.raw_rows([record])
        self.assertEqual(rows[0]["envelope_json"], "")
        self.assertEqual(
            json.loads(rows[0]["event_id"]), [record["topic"], 0, 4]
        )

    def test_logical_dates_and_timestamp_serialization(self):
        """Known typed source fields become parseable contract strings."""
        self.assertEqual(state.text_value(0, "planned_date"), "1970-01-01")
        self.assertEqual(
            state.text_value(1000, "updated_at"), "1970-01-01T00:00:01+00:00"
        )
        self.assertEqual(state.text_value(12, "good_qty"), "12")


class CdcSafetyTests(unittest.TestCase):
    """Exercise source and SQL-write boundaries before opening connections."""

    def test_retention_and_broker_reset_never_skip_history(self):
        """Both fresh and resumed consumers require their next exact offset."""
        self.assertEqual(state.assignment_offset(-1, 0, 0), 0)
        self.assertEqual(state.assignment_offset(4, 0, 5), 5)
        with self.assertRaises(RuntimeError):
            state.assignment_offset(-1, 1, 10)
        with self.assertRaises(RuntimeError):
            state.assignment_offset(4, 6, 10)
        with self.assertRaises(RuntimeError):
            state.assignment_offset(9, 0, 5)

    def test_connector_limits_columns_and_keeps_password_references(self):
        """Capture selects declared columns and requires verified TLS."""
        import connector

        values = config.settings(
            {
                "CDC_SQLSERVER_HOST": "company.example",
                "CDC_SQLSERVER_DATABASE": "company",
                "CDC_SQLSERVER_USER": "reader",
                "CDC_SQLSERVER_PASSWORD": "dummy-test-secret",
            }
        )
        mapping = {
            item["table"]: {
                "source_schema": "dbo",
                "source_table": item["table"],
                "columns": {column: column for column in item["columns"]},
            }
            for item in config.CONTRACTS
        }
        result = connector.configuration(values, mapping)
        self.assertEqual(result["driver.encrypt"], "true")
        self.assertEqual(result["driver.trustServerCertificate"], "false")
        self.assertNotIn("driver.trustStore", result)
        self.assertEqual(
            result["database.password"], "${env:CDC_SQLSERVER_PASSWORD}"
        )
        self.assertNotIn(values["CDC_SQLSERVER_PASSWORD"], json.dumps(result))
        selected = result["column.include.list"].split(",")
        self.assertEqual(
            len(selected),
            sum(len(item["columns"]) for item in config.CONTRACTS),
        )
        self.assertTrue(all(item.startswith("dbo\\.") for item in selected))

    def test_certificate_exception_is_demo_loopback_only(self):
        """Company sources cannot enable the test certificate bypass."""
        values = {
            "CDC_SQLSERVER_HOST": "company.example",
            "CDC_SQLSERVER_DATABASE": "company",
            "CDC_SQLSERVER_USER": "reader",
            "CDC_SQLSERVER_PASSWORD": "dummy",
            "CDC_TEST_TRUST_SERVER_CERTIFICATE": "true",
        }
        with self.assertRaises(ValueError):
            config.settings(values)
        values.update(
            CDC_SQLSERVER_HOST="localhost",
            CDC_SQLSERVER_DATABASE="lh_cdc_demo_test",
        )
        self.assertEqual(
            config.settings(values)["CDC_TEST_TRUST_SERVER_CERTIFICATE"],
            "true",
        )

    def test_fixed_dml_rejects_unowned_or_company_database(self):
        """Company names and prefixes alone cannot authorize writes."""
        specification = importlib.util.spec_from_file_location(
            "cdc_demo", ROOT / "scripts/cdc-demo.py"
        )
        demo = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(demo)
        with self.assertRaises(ValueError):
            demo.apply_scenario(
                {"CDC_SQLSERVER_DATABASE": "company"}, "delete"
            )
        with self.assertRaises(ValueError):
            demo.apply_scenario(
                {"CDC_SQLSERVER_DATABASE": "lh_cdc_demo_" + "a" * 16}, "insert"
            )
        with self.assertRaises(ValueError):
            demo.apply_scenario({}, "DROP TABLE")


if __name__ == "__main__":
    unittest.main()
