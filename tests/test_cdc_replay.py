"""Test pinned-raw recovery with real SQLite/Arrow and mocked services."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import cdc_replay  # noqa: E402
import config  # noqa: E402
import consumer  # noqa: E402
import state  # noqa: E402
from cdc_fixtures import (  # noqa: E402
    IDENTITY,
    MAPPING,
    TOPICS,
    ReplayCatalog,
    ReplayTable,
    complete_history,
    record,
)


class RawRebuildTests(unittest.TestCase):
    """Keep active state and mark partial derived outputs on failure."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cdc-raw-rebuild-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        self.store = state.State(self.path, IDENTITY)
        self.addCleanup(lambda: self.store.connection.close())
        self.records = complete_history()
        self.table = ReplayTable(self.records, self.store.get("identity"))
        self.admin = SimpleNamespace(
            catalog=lambda: ReplayCatalog(self.table),
            CONTRACT=config.CONTRACTS,
            STATE={"blocked": False},
        )
        self.materialize = mock.Mock(return_value={"sql_statements": 14})
        self.request = "d" * 32
        expressions = SimpleNamespace(
            EqualTo=lambda field, value: (field, value)
        )
        modules = {
            "pyiceberg": SimpleNamespace(expressions=expressions),
            "pyiceberg.expressions": expressions,
        }
        previous = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)

        def restore():
            """Preserve lazy Arrow/pandas imports while restoring our stubs."""
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)

    def rebuild(self, **options):
        """Run the real state/file protocol with only derived SQL mocked."""
        active, snapshot = cdc_replay.rebuild(
            self.admin,
            self.store,
            MAPPING,
            TOPICS,
            self.request,
            materialize=self.materialize,
            **options,
        )
        self.store = active
        return snapshot

    def test_rebuild_reconstructs_current_rows_deletes_tombstones_and_offsets(
        self,
    ):
        update = record(offset=1, operation="u", sequence=2, name="New")
        delete = record(offset=2, operation="d", sequence=3)
        tombstone = {**record(offset=3), "value": None}
        self.table.set_records([*self.records, update, delete, tombstone])
        snapshot = self.rebuild()
        self.assertEqual(snapshot, 42)
        self.assertEqual(self.store.rows("erp_products"), [])
        self.assertEqual(self.store.offset(delete["topic"], 0), 3)
        for target in MAPPING.keys() - {"erp_products"}:
            self.assertEqual(len(self.store.rows(target)), 1)
        self.assertTrue(self.store.get("initialized"))
        self.assertFalse(self.store.get("rebuild_pending"))
        self.assertEqual(self.store.get("rebuild_generation"), self.request)
        self.assertTrue(
            all(snapshot_id == 42 for snapshot_id, _, _ in self.table.calls)
        )
        self.materialize.assert_called_once()
        self.assertEqual(self.materialize.call_args.args[1].fixed_source, True)

    def test_raw_gap_missing_table_or_lost_completed_prefix_is_refused(self):
        cases = (
            [*self.records, record(offset=2, operation="u", sequence=2)],
            self.records[:-1],
            [],
        )
        for records in cases:
            with self.subTest(events=len(records)):
                self.table.set_records(records)
                with self.assertRaises(cdc_replay.RebuildError):
                    self.rebuild()
                self.assertEqual(self.store.completed_offsets(), {})
                self.assertIsNone(self.store.get("rebuild_pending"))
        self.table.set_records(self.records)
        self.store.complete([record(offset=1)])
        with self.assertRaisesRegex(cdc_replay.RebuildError, "INCOMPLETE"):
            self.rebuild()
        self.materialize.assert_not_called()

    def test_source_identity_and_unknown_topics_cannot_be_claimed(self):
        self.table.properties["lakehouse.fixed-cdc.identity"] = "e" * 64
        with self.assertRaisesRegex(
            cdc_replay.RebuildError, "SOURCE_MISMATCH"
        ):
            self.rebuild()
        self.table.properties["lakehouse.fixed-cdc.identity"] = (
            consumer.identity_digest(IDENTITY)
        )
        self.table.set_records(
            [
                *self.records,
                {
                    **record(),
                    "topic": "unrelated.example-topic",
                },
            ]
        )
        with self.assertRaisesRegex(
            cdc_replay.RebuildError, "SOURCE_MISMATCH"
        ):
            self.rebuild()
        self.materialize.assert_not_called()

    def test_limits_and_malformed_raw_leave_active_state_untouched(self):
        for options in ({"maximum": 1}, {"byte_limit": 1}):
            with self.assertRaises(cdc_replay.RebuildError):
                self.rebuild(**options)
            self.assertEqual(self.store.completed_offsets(), {})
        bad = copy.deepcopy(self.records)
        bad[0]["value"] = ""
        self.table.set_records(bad)
        with self.assertRaises(ValueError):
            self.rebuild()
        self.assertEqual(self.store.rows("erp_products"), [])
        self.assertIsNone(self.store.get("rebuild_pending"))
        self.materialize.assert_not_called()
        self.assertFalse(list(self.path.parent.glob(".rebuild-*")))

    def test_partial_materialization_keeps_pending_marker_and_old_checkpoint(
        self,
    ):
        self.materialize.side_effect = RuntimeError(
            "synthetic-private-SQL-detail"
        )
        with self.assertRaises(RuntimeError):
            self.rebuild()
        self.assertTrue(self.store.get("rebuild_pending"))
        self.assertEqual(self.store.completed_offsets(), {})
        self.assertEqual(self.store.rows("erp_products"), [])
        self.store.connection.close()
        self.store = state.State(self.path, IDENTITY)
        self.assertTrue(self.store.get("rebuild_pending"))

    def test_unknown_query_cancellation_cannot_be_cleared_by_rebuild(self):
        self.store.set("blocked", "true")
        with self.assertRaisesRegex(cdc_replay.RebuildError, "QUERY_REVIEW"):
            self.rebuild()
        self.assertEqual(self.store.get("blocked"), "true")
        self.materialize.assert_not_called()

    def test_resume_validation_precedes_any_derived_write_or_pending_marker(
        self,
    ):
        with self.assertRaises(RuntimeError):
            self.rebuild(validate_resume=mock.Mock(side_effect=RuntimeError))
        self.assertIsNone(self.store.get("rebuild_pending"))
        self.assertEqual(self.store.completed_offsets(), {})
        self.materialize.assert_not_called()

    def test_failed_atomic_publish_preserves_old_file_and_pending_generation(
        self,
    ):
        with mock.patch.object(cdc_replay.os, "replace", side_effect=OSError):
            with self.assertRaises(OSError):
                self.rebuild()
        self.store = state.State(self.path, IDENTITY)
        self.assertEqual(self.store.completed_offsets(), {})
        self.assertTrue(self.store.get("rebuild_pending"))
        self.assertFalse(list(self.path.parent.glob(".rebuild-*")))

    def test_null_and_empty_values_are_distinct_in_replay_validation(self):
        rows = copy.deepcopy(self.table.pinned)
        rows[0]["envelope_json"] = ""
        rows[0]["tombstone"] = "true"
        self.table.pinned = rows
        with self.assertRaisesRegex(
            cdc_replay.RebuildError, "PAYLOAD_INVALID"
        ):
            self.rebuild()
        self.materialize.assert_not_called()

    def test_rebuild_preserves_durable_diagnostics_from_prior_attempts(self):
        self.store.record_failure(
            self.records[0], "CDC_MATERIALIZATION_FAILED"
        )
        prior = "a" * 32
        self.store.record_rebuild(prior, "failed", error="CDC_REBUILD_FAILED")
        self.rebuild()
        self.assertEqual(
            self.store.connection.execute(
                "SELECT error_code FROM processing_errors"
            ).fetchall(),
            [("CDC_MATERIALIZATION_FAILED",)],
        )
        history = dict(
            self.store.connection.execute(
                "SELECT request_id,status FROM rebuild_history"
            ).fetchall()
        )
        self.assertEqual(history[prior], "failed")
        self.assertEqual(history[self.request], "succeeded")


if __name__ == "__main__":
    unittest.main()
