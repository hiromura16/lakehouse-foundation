"""Test typed fixed CDC meaning and durable guards without live services."""

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import consumer  # noqa: E402
import state  # noqa: E402
from cdc_fixtures import (  # noqa: E402
    IDENTITY,
    MAPPING,
    ORIGINAL_TIME,
    TOPICS,
    VALUES,
    change_envelope,
    record,
)


class FixedStateTests(unittest.TestCase):
    """Exercise real SQLite durability, not only generated SQL strings."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fixed-cdc-state-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.store = state.State(self.path, IDENTITY)
        self.addCleanup(lambda: self.store.connection.close())

    def stage(self, message):
        """Apply one synthetic product event through the production decoder."""
        self.store.stage(
            message,
            "erp_products",
            MAPPING["erp_products"],
            list(MAPPING["erp_products"]["columns"]),
        )

    def test_original_null_empty_japanese_decimal_and_revision_are_preserved(
        self,
    ):
        self.stage(record())
        row = self.store.rows("erp_products")[0]
        self.assertEqual(row["product_name"], "日本語の製品")
        self.assertIsNone(row["product_family"])
        self.assertEqual(row["unit"], "")
        self.assertEqual(row["standard_cycle_seconds"], "123456789.123")
        self.assertEqual(row["updated_at"], ORIGINAL_TIME)
        self.assertEqual(
            row["_source_updated_at"], "2026-04-01T12:00:00.1234567Z"
        )
        self.assertEqual(row["_source_schema"], "lh_demo_erp")
        self.assertEqual(row["_source_table"], "products")
        self.assertEqual(row["_run_id"], "012345abcdef")

    def test_nanoseconds_preserve_seventh_digit_and_negative_epoch(self):
        self.assertEqual(
            state.nano_timestamp(1000000100), "1970-01-01T00:00:01.0000001Z"
        )
        self.assertEqual(
            state.nano_timestamp(-100), "1969-12-31T23:59:59.9999999Z"
        )
        for invalid in (True, 1.0, "100", 1, 2**63):
            with self.subTest(kind=type(invalid).__name__):
                with self.assertRaises(ValueError):
                    state.nano_timestamp(invalid)

    def test_snapshot_accepts_nullable_lsn_then_streaming_revision_wins(self):
        self.stage(record())
        self.stage(record(offset=1, operation="u", sequence=2, name="Updated"))
        self.stage(record(offset=2, name="Late snapshot"))
        self.assertEqual(
            self.store.rows("erp_products")[0]["product_name"], "Updated"
        )

    def test_equal_lsn_uses_event_serial_number_and_old_delete_replay_is_safe(
        self,
    ):
        first = record(offset=0, operation="u", sequence=1, name="First")
        second = change_envelope(
            record(offset=1, operation="u", sequence=1, name="Second"),
            lambda body: body["source"].update(event_serial_no=2),
        )
        self.stage(first)
        self.stage(second)
        self.assertEqual(
            self.store.rows("erp_products")[0]["product_name"], "Second"
        )
        self.stage(record(offset=2, operation="d", sequence=3))
        self.stage(record(offset=3, operation="u", sequence=2, name="Old"))
        self.assertEqual(self.store.rows("erp_products"), [])
        self.stage(
            record(offset=4, operation="c", sequence=4, name="Reinsert")
        )
        self.assertEqual(
            self.store.rows("erp_products")[0]["product_name"], "Reinsert"
        )

    def test_update_before_lob_and_delete_before_lob_may_be_null(self):
        self.stage(record())
        for operation, sequence in (("u", 2), ("d", 3)):
            message = change_envelope(
                record(
                    offset=sequence,
                    operation=operation,
                    sequence=sequence,
                    name="Changed",
                ),
                lambda body: body["before"].update(original_values=None),
            )
            self.stage(message)
        self.assertEqual(self.store.rows("erp_products"), [])

    def test_tombstone_is_not_a_business_delete(self):
        self.stage(record())
        self.stage({**record(offset=1), "value": None})
        self.assertEqual(len(self.store.rows("erp_products")), 1)

    def test_missing_sidecar_or_conflicting_source_and_key_are_refused(self):
        changes = (
            lambda body: body["after"].update(original_values=None),
            lambda body: body["after"].update(product_id="other-key"),
            lambda body: body["source"].update(db="other_database"),
            lambda body: body["after"].update(
                original_values=json.dumps(
                    {
                        **json.loads(body["after"]["original_values"]),
                        "product_id": "other-key",
                    }
                )
            ),
        )
        for change in changes:
            with self.assertRaises(ValueError):
                self.stage(change_envelope(record(), change))
        self.assertEqual(self.store.rows("erp_products"), [])

    def test_partial_materialization_records_safe_error_without_checkpoint(
        self,
    ):
        message = record()
        admin = SimpleNamespace(catalog=lambda: None, STATE={"blocked": False})
        with (
            mock.patch.object(consumer, "append_raw", return_value=1),
            mock.patch.object(
                consumer,
                "materialize",
                side_effect=RuntimeError("private SQL detail"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                consumer.process(
                    admin,
                    self.store,
                    [message],
                    MAPPING,
                    TOPICS,
                    values=VALUES,
                )
        self.assertEqual(self.store.offset(message["topic"], 0), -1)
        self.assertEqual(self.store.get("materialization_pending"), "true")
        saved = self.store.connection.execute(
            "SELECT error_code FROM processing_errors"
        ).fetchall()
        self.assertEqual(saved, [("CDC_MATERIALIZATION_FAILED",)])

    def test_valid_then_invalid_staging_is_durably_pending_before_any_sql(
        self,
    ):
        valid = record()
        invalid = change_envelope(
            record("erp_work_orders"),
            lambda body: body["after"].update(original_values=None),
        )
        admin = SimpleNamespace(catalog=lambda: None, STATE={"blocked": False})
        with (
            mock.patch.object(consumer, "append_raw", return_value=2),
            mock.patch.object(consumer, "materialize") as materialize,
        ):
            with self.assertRaises(ValueError):
                consumer.process(
                    admin,
                    self.store,
                    [valid, invalid],
                    MAPPING,
                    TOPICS,
                    values=VALUES,
                )
        self.assertEqual(len(self.store.rows("erp_products")), 1)
        self.assertEqual(self.store.completed_offsets(), {})
        self.assertEqual(self.store.get("materialization_pending"), "true")
        materialize.assert_not_called()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT error_code FROM processing_errors"
            ).fetchall(),
            [("CDC_INVALID_EVENT",)],
        )

    def test_missing_identity_cannot_claim_initialized_or_current_state(self):
        self.stage(record())
        self.store.set("initialized", "true")
        with self.store.connection:
            self.store.connection.execute(
                "DELETE FROM metadata WHERE key='identity'"
            )
        self.store.connection.close()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity and schema"):
            state.State(self.path, IDENTITY)
        self.assertEqual(self.path.read_bytes(), before)
        # Reopen only for cleanup; inspect directly without claiming identity.
        self.store.connection = sqlite3.connect(self.path)
        self.assertIsNone(self.store.get("identity"))

    def test_unrelated_sqlite_file_is_unchanged_when_refused(self):
        foreign = self.path.with_name("unrelated.sqlite3")
        connection = sqlite3.connect(foreign)
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.commit()
        connection.close()
        before = foreign.read_bytes()
        with self.assertRaises(ValueError):
            state.State(foreign, IDENTITY)
        self.assertEqual(foreign.read_bytes(), before)

    def test_generic_identity_cannot_be_reused_as_fixed_state(self):
        other = copy.deepcopy(IDENTITY)
        other["installation_id"] = "c" * 32
        with self.assertRaises(ValueError):
            state.State(self.path, other)
        self.assertEqual(
            self.store.get("identity"), consumer.identity_digest(IDENTITY)
        )

    def test_state_budget_failure_keeps_prior_rows_and_offset(self):
        self.stage(record())
        before = self.store.rows("erp_products")
        with mock.patch.object(state, "MAX_CURRENT_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "byte limit"):
                self.stage(record(offset=1, operation="u", sequence=2))
        self.assertEqual(self.store.rows("erp_products"), before)
        self.assertEqual(self.store.offset(record()["topic"], 0), -1)


if __name__ == "__main__":
    unittest.main()
