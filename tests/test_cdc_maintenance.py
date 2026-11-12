"""Exercise the paused CDC run loop with a fake clock and broker only.

No SQL, Kafka, Docker or HTTP listener is opened. A dedicated stop exception
ends finite iterations while the real run-loop cleanup paths are exercised.
"""

import io
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import consumer  # noqa: E402
import consumer_runtime  # noqa: E402
import runtime_control  # noqa: E402


class StopLoop(Exception):
    """Finish a bounded fake run without adding production stop behavior."""


class Clock:
    """Accumulate exact millisecond ticks instead of floating-point drift."""

    def __init__(self):
        self.milliseconds = 0

    def monotonic(self):
        return self.milliseconds / 1000

    def advance(self, seconds):
        self.milliseconds += round(seconds * 1000)


class FakeBroker:
    """Record every group poll; never stage messages or acknowledge offsets."""

    def __init__(self, clock, iterations, *, fail_first=False):
        self.clock = clock
        self.iterations = iterations
        self.fail_first = fail_first
        self.polls = []
        self.callbacks = []
        self.close = mock.Mock()
        self.pause = mock.Mock()
        self.commit = mock.Mock()
        self.subscribe = mock.Mock()
        self.after_poll = lambda: None

    def poll(self, wait):
        if len(self.polls) >= self.iterations:
            raise StopLoop()
        self.polls.append(wait)
        self.clock.advance(wait)
        self.callbacks.append(self.clock.monotonic())
        self.after_poll()
        if self.fail_first and len(self.polls) == 1:
            return SimpleNamespace(
                error=lambda: SimpleNamespace(code=lambda: "synthetic_error")
            )
        return None


class MaintenanceTests(unittest.TestCase):
    """Check real loop wiring while mocking all external/runtime services."""

    def run_loop(
        self, *, interval=1, iterations=12, fail_first=False, rebuild_at=None
    ):
        clock = Clock()
        broker = FakeBroker(clock, iterations, fail_first=fail_first)
        observations = []
        rebuild_checks = []
        runtime = SimpleNamespace(
            maintenance=not fail_first,
            partitions=["synthetic_partition"],
            assign=mock.Mock(),
            state=SimpleNamespace(
                connection=SimpleNamespace(close=mock.Mock())
            ),
            process=mock.Mock(),
        )
        runtime.observe = lambda **flags: observations.append(
            (clock.monotonic(), flags)
        )
        rebuilt = False

        def handle_rebuild():
            nonlocal rebuilt
            rebuild_checks.append(clock.monotonic())
            if rebuild_at is None or rebuilt or len(broker.polls) < rebuild_at:
                return False
            rebuilt = True
            # Real Runtime publishes both busy and terminal observations;
            # this synthetic outcome remains paused after a failed rebuild.
            runtime.observe(busy=True)
            runtime.observe()
            return True

        runtime.handle_rebuild = handle_rebuild

        def runtime_factory(*args, **kwargs):
            del args, kwargs
            runtime.observe()
            return runtime

        values = {
            "CDC_SOURCE_MODE": "fixed-demo",
            "CDC_SQLSERVER_DATABASE": "synthetic_database",
            "CDC_SQLSERVER_HOST": "source.example.invalid",
            "CDC_SQLSERVER_PORT": "1433",
            "CDC_TOPIC_PREFIX": "synthetic_cdc",
            "CDC_GROUP_ID": "synthetic_group",
            "CDC_BATCH_SIZE": "100",
            "CDC_POLL_SECONDS": str(interval),
            "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
        }
        state = SimpleNamespace(get=lambda key: "", connection=runtime.state)
        server = SimpleNamespace(
            shutdown=mock.Mock(), server_close=mock.Mock()
        )
        control = SimpleNamespace(snapshot=lambda: {"ready": False})
        dependencies = {
            "admin": SimpleNamespace(),
            "confluent_kafka": SimpleNamespace(
                Consumer=mock.Mock(return_value=broker),
                KafkaError=SimpleNamespace(UNKNOWN_TOPIC_OR_PART="missing"),
                TopicPartition=mock.Mock(),
            ),
        }
        # Restore only our injected names; blanket sys.modules restoration
        # would discard unrelated lazy imports used by the full test suite.
        previous = {name: sys.modules.get(name) for name in dependencies}
        sys.modules.update(dependencies)
        try:
            with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        consumer, "settings", return_value=values
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        consumer, "selected_mapping", return_value={}
                    )
                )
                stack.enter_context(
                    mock.patch.object(consumer, "topic_names", return_value={})
                )
                stack.enter_context(mock.patch.object(consumer, "preflight"))
                stack.enter_context(
                    mock.patch.object(consumer, "State", return_value=state)
                )
                stack.enter_context(
                    mock.patch.object(consumer, "refuse_lost_state")
                )
                stack.enter_context(
                    mock.patch.object(
                        consumer_runtime, "Runtime", runtime_factory
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runtime_control, "Control", return_value=control
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        runtime_control, "start_server", return_value=server
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        consumer.time, "monotonic", clock.monotonic
                    )
                )
                stack.enter_context(redirect_stdout(io.StringIO()))
                with self.assertRaises(StopLoop):
                    consumer.run(Path(folder), "synthetic-unused-map.json")
        finally:
            for name, module in previous.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        broker.close.assert_called_once()
        runtime.state.connection.close.assert_called_once()
        server.shutdown.assert_called_once()
        server.server_close.assert_called_once()
        runtime.process.assert_not_called()
        broker.commit.assert_not_called()
        return observations, rebuild_checks, broker

    def test_sampling_is_bounded_but_group_callbacks_remain_frequent(self):
        observations, checks, broker = self.run_loop(iterations=12)
        self.assertEqual([stamp for stamp, _ in observations], [0, 1, 2])
        self.assertEqual(broker.polls, [0.2] * 12)
        self.assertEqual(len(broker.callbacks), 12)
        self.assertEqual(len(checks), 13)

    def test_configured_long_interval_does_not_delay_callbacks(self):
        observations, _, broker = self.run_loop(interval=5, iterations=31)
        self.assertEqual([stamp for stamp, _ in observations], [0, 5])
        self.assertEqual(broker.polls, [0.2] * 31)

    def test_transition_observes_immediately_then_starts_sampling_interval(
        self,
    ):
        observations, _, broker = self.run_loop(iterations=8, fail_first=True)
        self.assertEqual([stamp for stamp, _ in observations], [0, 1, 2])
        self.assertEqual(broker.polls, [1] + [0.2] * 7)
        broker.pause.assert_called_once()

    def test_rebuild_remains_responsive_and_resets_sampling_deadline(self):
        observations, checks, broker = self.run_loop(
            iterations=10, rebuild_at=3
        )
        self.assertEqual(
            observations,
            [(0, {}), (0.6, {"busy": True}), (0.6, {}), (1.6, {})],
        )
        self.assertIn(0.6, checks)
        self.assertEqual(broker.polls, [0.2] * 10)

    def test_slow_sampling_schedules_from_completion_without_catchup(self):
        clock = Clock()
        broker = FakeBroker(clock, 2)
        runtime = SimpleNamespace(
            observe=mock.Mock(side_effect=lambda: clock.advance(3))
        )
        with mock.patch.object(consumer.time, "monotonic", clock.monotonic):
            deadline = consumer.poll_maintenance(broker, runtime, 0, 5)
            self.assertEqual(deadline, 8.2)
            self.assertEqual(
                consumer.poll_maintenance(broker, runtime, deadline, 5),
                deadline,
            )
        runtime.observe.assert_called_once()
        self.assertEqual(broker.polls, [0.2, 0.2])


class SqlReplacementScopeTests(unittest.TestCase):
    """Keep destructive SQL maintenance explicit and ordered before removal."""

    def test_unknown_inventory_blocks_before_enable_or_disable(self):
        sql = ROOT / "infra/sqlserver/maintenance/replace_legacy_captures.sql"
        body = sql.read_text()
        self.assertLess(
            body.index("THROW 51204"),
            body.index("EXEC sys.sp_executesql @LegacyEnableSql"),
        )
        self.assertLess(
            body.index("THROW 51206"),
            body.index("EXEC @LegacyResult=sys.sp_cdc_disable_table"),
        )
        self.assertEqual(
            body.count("EXEC sys.sp_executesql @LegacyEnableSql"), 2
        )
        self.assertIn("THROW 51208", body)
        self.assertIn("THROW 51209", body)
        self.assertIn("THROW 51210", body)
        self.assertIn("preflight_presence", body)
        self.assertIn("current_presence", body)
        self.assertIn("preserve_state_review_then_rerun_same_allowlist", body)
        self.assertNotIn("sp_cdc_disable_db", body)
        self.assertNotIn("sp_delete_job", body)
        self.assertNotIn("sp_cdc_drop_job", body)
        for name in ("004_demo_cdc_enable.sql", "006_demo_validation.sql"):
            normal = (ROOT / "infra/sqlserver" / name).read_text()
            self.assertNotIn("@LegacyApproved", normal)
            self.assertNotIn("replace_legacy", normal)


if __name__ == "__main__":
    unittest.main()
