"""Test CDC thread boundaries with fake services and real local state."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import config  # noqa: E402
import consumer  # noqa: E402
import consumer_runtime  # noqa: E402
import runtime_control  # noqa: E402
import state  # noqa: E402
from cdc_fixtures import (  # noqa: E402
    IDENTITY,
    MAPPING,
    TOPICS,
    VALUES,
    Broker,
    ReplayCatalog,
    ReplayTable,
    complete_history,
    partition,
)


class RuntimeTests(unittest.TestCase):
    """Distinguish queued work, local commits and broker acknowledgements."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="cdc-runtime-test-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "state.sqlite3"
        store = state.State(self.path, IDENTITY)
        store.set("initialized", "true")
        self.records = complete_history()
        self.table = ReplayTable(self.records, store.get("identity"))
        self.admin = SimpleNamespace(
            catalog=lambda: ReplayCatalog(self.table),
            CONTRACT=config.CONTRACTS,
            STATE={"blocked": False},
        )
        self.broker = Broker(self.records)
        self.control = runtime_control.Control("fixed-demo", "t" * 64, MAPPING)
        self.materialize = mock.Mock(return_value={"sql_statements": 14})
        self.runtime = consumer_runtime.Runtime(
            self.admin,
            store,
            VALUES,
            MAPPING,
            TOPICS,
            self.control,
            self.broker,
            partition,
            consumer.process,
            self.materialize,
        )
        self.addCleanup(lambda: self.runtime.state.connection.close())
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
            """Restore SDK stubs while keeping Arrow lazy imports."""
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)

    def assign(self):
        """Observe only the actual synthetic topic partitions, not aliases."""
        self.runtime.assign(
            self.broker, [partition(row["topic"], 0) for row in self.records]
        )

    def queue(self):
        """Receive an HTTP-equivalent 202 while doing no state/broker work."""
        status, request = self.control.dispatch(
            "POST", "/rebuild", "Bearer " + "t" * 64, b'{"confirm":true}'
        )
        self.assertEqual(status, 202)
        self.assertFalse(request["completed"])
        return request["request_id"]

    def test_startup_needs_assignment_and_a_completed_derived_generation(self):
        self.assertFalse(self.control.snapshot()["ready"])
        self.assertEqual(self.control.snapshot()["error_code"], "CDC_STARTING")
        self.assign()
        self.assertFalse(self.control.snapshot()["ready"])
        self.runtime.state.complete(self.records)
        self.runtime.observe()
        self.assertTrue(self.control.snapshot()["ready"])
        self.assertTrue(
            all(
                row["lag"] == 0
                for row in self.control.snapshot()["partitions"]
            )
        )

    def test_broker_observation_failure_is_unknown_and_not_ready(self):
        self.runtime.state.complete(self.records)
        self.assign()
        with mock.patch.object(
            self.broker,
            "get_watermark_offsets",
            side_effect=RuntimeError("private broker text"),
        ):
            self.runtime.observe()
        snapshot = self.control.snapshot()
        self.assertFalse(snapshot["ready"])
        for row in snapshot["partitions"]:
            self.assertEqual(row["state"], "unavailable")
            self.assertIsNone(row["broker_high"])
            self.assertIsNone(row["lag"])
        self.assertNotIn("private broker", json.dumps(snapshot))

    def test_retention_gap_pauses_fixed_delivery_and_leaves_rebuild_api_alive(
        self,
    ):
        for key in self.broker.bounds:
            self.broker.bounds[key] = (1, 2)
        self.assign()
        self.assertTrue(self.runtime.maintenance)
        self.assertFalse(self.control.snapshot()["ready"])
        self.assertEqual(
            self.control.snapshot()["error_code"], "CDC_REBUILD_REQUIRED"
        )
        self.assertTrue(any(call[0] == "pause" for call in self.broker.calls))
        self.assertFalse(
            any(call[0] == "commit" for call in self.broker.calls)
        )
        self.queue()

    def test_generic_assignment_still_refuses_missing_broker_history(self):
        identity = {
            name: value
            for name, value in IDENTITY.items()
            if name not in {"source_mode", "schema_version", "installation_id"}
        }
        generic = state.State(self.path.with_name("generic.sqlite3"), identity)
        self.addCleanup(generic.connection.close)
        control = runtime_control.Control("generic", "t" * 64, MAPPING)
        runtime = consumer_runtime.Runtime(
            self.admin,
            generic,
            {**VALUES, "CDC_SOURCE_MODE": "generic"},
            MAPPING,
            TOPICS,
            control,
            self.broker,
            partition,
            consumer.process,
            self.materialize,
        )
        topic = self.records[0]["topic"]
        self.broker.bounds[(topic, 0)] = (1, 2)
        with self.assertRaises(RuntimeError):
            runtime.assign(self.broker, [partition(topic, 0)])

    def test_pending_generation_survives_restart_without_consuming_or_ack(
        self,
    ):
        self.runtime.state.set("materialization_pending", "true")
        self.runtime.state.connection.close()
        self.runtime.state = state.State(self.path, IDENTITY)
        self.runtime = consumer_runtime.Runtime(
            self.admin,
            self.runtime.state,
            VALUES,
            MAPPING,
            TOPICS,
            self.control,
            self.broker,
            partition,
            consumer.process,
            self.materialize,
        )
        self.assign()
        self.assertTrue(self.runtime.maintenance)
        self.assertFalse(self.control.snapshot()["ready"])
        self.assertEqual(self.runtime.state.completed_offsets(), {})
        self.assertFalse(
            any(call[0] == "commit" for call in self.broker.calls)
        )

    def test_http_dispatch_reads_only_cache_and_queues_without_touching_state(
        self,
    ):
        with mock.patch.object(
            self.runtime.state,
            "counts",
            side_effect=AssertionError("HTTP read SQLite"),
        ):
            before = list(self.broker.calls)
            self.control.dispatch("GET", "/status", "Bearer " + "t" * 64)
            self.queue()
            self.assertEqual(self.broker.calls, before)
            self.assertEqual(
                self.control.snapshot()["rebuild"]["status"], "queued"
            )

    def test_checkpoint_is_durable_before_successful_broker_ack(self):
        self.assign()
        original_commit = self.broker.commit

        def commit(*, offsets, asynchronous):
            """Observe SQLite at the external acknowledgement boundary."""
            self.assertTrue(
                all(
                    self.runtime.state.offset(item.topic, item.partition) == 0
                    for item in offsets
                )
            )
            return original_commit(offsets=offsets, asynchronous=asynchronous)

        with (
            mock.patch.object(consumer, "append_raw", return_value=6),
            mock.patch.object(
                consumer, "materialize", return_value={"sql_statements": 14}
            ),
            mock.patch.object(self.broker, "commit", side_effect=commit),
        ):
            result = self.runtime.process(self.records)
        self.assertEqual(result["raw_events_appended"], 6)
        self.assertEqual(
            self.control.snapshot()["raw_events_appended_since_start"], 6
        )
        self.assertTrue(self.control.snapshot()["ready"])
        self.assertFalse(self.runtime.state.get("materialization_pending"))

    def test_partial_sql_failure_halts_without_checkpoint_or_broker_ack(self):
        self.assign()
        with (
            mock.patch.object(consumer, "append_raw", return_value=6),
            mock.patch.object(
                consumer,
                "materialize",
                side_effect=RuntimeError("private driver text"),
            ),
        ):
            result = self.runtime.process(self.records)
        self.assertIsNone(result)
        self.assertEqual(self.runtime.state.completed_offsets(), {})
        self.assertFalse(
            any(call[0] == "commit" for call in self.broker.calls)
        )
        self.assertEqual(
            self.control.snapshot()["error_code"], "CDC_PROCESSING_FAILED"
        )
        self.assertFalse(self.control.snapshot()["ready"])

    def test_broker_ack_failure_keeps_valid_local_checkpoint_and_counts_once(
        self,
    ):
        self.assign()
        self.broker.fail_commit = True
        with (
            mock.patch.object(consumer, "append_raw", return_value=6),
            mock.patch.object(
                consumer, "materialize", return_value={"sql_statements": 14}
            ),
        ):
            result = self.runtime.process(self.records)
        self.assertIsNone(result)
        self.assertEqual(len(self.runtime.state.completed_offsets()), 6)
        self.assertEqual(
            self.control.snapshot()["error_code"], "CDC_KAFKA_ACK_FAILED"
        )
        self.assertEqual(
            self.control.snapshot()["raw_events_appended_since_start"], 6
        )
        self.assertFalse(self.control.snapshot()["ready"])

    def test_raw_failure_on_completed_offset_is_not_a_broker_ack_failure(self):
        self.runtime.state.complete(self.records)
        self.assign()
        with mock.patch.object(
            consumer,
            "append_raw",
            side_effect=ValueError("synthetic-private-changed-payload"),
        ):
            self.assertIsNone(self.runtime.process(self.records))
        self.assertEqual(
            self.control.snapshot()["error_code"], "CDC_PROCESSING_FAILED"
        )
        codes = self.runtime.state.connection.execute(
            "SELECT DISTINCT error_code FROM processing_errors"
        ).fetchall()
        self.assertEqual(codes, [("CDC_RAW_WRITE_FAILED",)])
        self.assertFalse(
            any(call[0] == "commit" for call in self.broker.calls)
        )

    def test_rebuild_publishes_then_seeks_acks_and_resumes_with_matching_id(
        self,
    ):
        self.assign()
        request_id = self.queue()
        self.assertTrue(self.runtime.handle_rebuild())
        rebuilt = self.control.snapshot()["rebuild"]
        self.assertEqual(rebuilt["request_id"], request_id)
        self.assertEqual(rebuilt["status"], "succeeded")
        self.assertTrue(rebuilt["completed"])
        self.assertEqual(rebuilt["raw_snapshot_id"], 42)
        calls = [row[0] for row in self.broker.calls]
        self.assertLess(calls.index("pause"), calls.index("seek"))
        self.assertLess(calls.index("seek"), calls.index("commit"))
        self.assertLess(calls.index("commit"), calls.index("resume"))
        self.assertEqual(
            self.runtime.state.get("rebuild_generation"), request_id
        )
        self.assertTrue(self.control.snapshot()["ready"])

    def test_rebuild_failure_retains_durable_pending_and_never_acknowledges(
        self,
    ):
        self.assign()
        self.materialize.side_effect = RuntimeError("private query failure")
        self.queue()
        self.runtime.handle_rebuild()
        self.assertEqual(self.runtime.state.completed_offsets(), {})
        self.assertTrue(self.runtime.state.get("rebuild_pending"))
        self.assertFalse(
            any(call[0] == "commit" for call in self.broker.calls)
        )
        rebuilt = self.control.snapshot()["rebuild"]
        self.assertEqual(rebuilt["status"], "failed")
        self.assertFalse(rebuilt["completed"])
        self.assertEqual(rebuilt["error_code"], "CDC_REBUILD_FAILED")
        self.assertNotIn("private query", json.dumps(self.control.snapshot()))

    def test_ack_failure_after_rebuild_retains_published_local_generation(
        self,
    ):
        self.assign()
        self.broker.fail_commit = True
        request_id = self.queue()
        self.runtime.handle_rebuild()
        self.assertEqual(
            self.runtime.state.get("rebuild_generation"), request_id
        )
        self.assertEqual(len(self.runtime.state.completed_offsets()), 6)
        self.assertFalse(self.runtime.state.get("rebuild_pending"))
        self.assertEqual(
            self.control.snapshot()["error_code"], "CDC_KAFKA_ACK_FAILED"
        )
        self.assertFalse(self.control.snapshot()["ready"])
        self.assertFalse(self.control.snapshot()["rebuild"]["completed"])

    def test_public_status_excludes_source_identifiers_and_business_data(
        self,
    ):
        self.assign()
        text = json.dumps(self.control.snapshot())
        for private in (
            VALUES["SOURCE_SQLSERVER_HOST"],
            VALUES["SOURCE_SQLSERVER_DATABASE"],
            VALUES["SOURCE_SQLSERVER_USER"],
            VALUES["SOURCE_SQLSERVER_PASSWORD"],
            "product_name",
        ):
            self.assertNotIn(private, text)


if __name__ == "__main__":
    unittest.main()
