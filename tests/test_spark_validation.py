"""Test smoke evidence and HTTP/lock contracts without Spark or a database.

Network, Spark and Iceberg calls are synthetic fakes. A separate class uses
only owned temporary Linux files to verify the actual no-follow/flock path.
No host notebook preparation, Docker deployment or real smoke write occurs.
"""

import contextlib
import copy
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/compute"))
RESULT = importlib.import_module("smoke_result")
SMOKE = importlib.import_module("spark_smoke")
NEXT = "http://trino:8080/v1/statement/executing/synthetic/1"


class Response(io.BytesIO):
    """Expose HTTPResponse's single-read interface with bounded fake bytes."""

    def __init__(self, payload, *, raw=False, status=200, headers=None):
        data = payload if raw else json.dumps(payload).encode()
        super().__init__(data)
        self.status = status
        self.headers = headers or {}


class Frame:
    """Return exact synthetic Decimal rows without importing PySpark."""

    def __init__(self, rows=None):
        self.rows = SMOKE.EXPECTED if rows is None else rows

    def take(self, limit):
        return [
            SimpleNamespace(id=row[0], label=row[1], amount=Decimal(row[2]))
            for row in self.rows[:limit]
        ]


class Spark:
    """Record synchronous fixed SQL and cleanup order without a JVM."""

    def __init__(self, events):
        self.sparkContext = SimpleNamespace(
            master="local[2]", setLogLevel=mock.Mock()
        )
        self.version = "4.1.4"
        self.events = events
        self.sql = mock.Mock(side_effect=self.execute)
        self.stop = mock.Mock(side_effect=lambda: events.append("stop"))

    def execute(self, statement):
        self.events.append(statement)
        return Frame()


def ready(result):
    """Stand in for cluster observations in pipeline-only unit tests."""
    RESULT.set_stage(result, "master_reachability", "passed")
    RESULT.set_stage(result, "worker_registration", "passed")


class ResultTests(unittest.TestCase):
    """Missing evidence cannot become an overall success."""

    def result(self, *, local=False):
        return RESULT.new_result({}, local_only=local, run_id="a" * 32)

    def test_initial_contract_is_unknown_not_run_and_safe(self):
        result = self.result()
        self.assertEqual(set(result["stages"]), set(RESULT.STAGES))
        self.assertTrue(
            all(
                item["status"] == "not_run"
                for item in result["stages"].values()
            )
        )
        self.assertEqual(result["source_commit"], "unknown")
        self.assertEqual(result["image_identity"], "unknown")
        self.assertIsNone(result["value_match"])
        self.assertFalse(result["distributed_executors_verified"])
        record = RESULT.result_record(result)
        self.assertTrue(record.startswith(RESULT.RESULT_PREFIX))
        self.assertEqual(json.loads(record.split("=", 1)[1]), result)

    def test_metadata_selects_only_supplied_safe_provenance(self):
        values = {
            "SPARK_SMOKE_SOURCE_COMMIT": "b" * 40,
            "SPARK_SMOKE_IMAGE_ID": "sha256:" + "c" * 64,
            "SPARK_SMOKE_CONFIGURED_PROFILES": "demo,monitoring,demo",
            "SPARK_SMOKE_COMMAND_PROFILES": "compute",
            "SPARK_SMOKE_DIRTY_STATUS": "dirty",
            "PASSWORD": "not-public",
        }
        result = RESULT.new_result(values)
        self.assertEqual(result["configured_profiles"], ["demo", "monitoring"])
        self.assertEqual(result["command_enabled_profiles"], ["compute"])
        self.assertEqual(result["source_commit"], "b" * 40)
        self.assertNotIn("not-public", json.dumps(result))
        with self.assertRaises(RESULT.SmokeFailure):
            RESULT.new_result({"SPARK_SMOKE_RUN_ID": "../not-a-run"})
        with self.assertRaises(RESULT.SmokeFailure):
            RESULT.new_result({"SPARK_SMOKE_CONFIGURED_PROFILES": "../bad"})

    def test_finish_refuses_all_skipped_or_missing_counts_and_cleanup(self):
        result = self.result()
        for stage in RESULT.STAGES:
            RESULT.set_stage(result, stage, "skipped")
        RESULT.finish_result(result, time.monotonic())
        self.assertEqual(result["overall_status"], "failed")
        result = self.result()
        for stage in RESULT.STAGES:
            RESULT.set_stage(result, stage, "passed")
        RESULT.finish_result(result, time.monotonic())
        self.assertEqual(result["exit_code"], 1)

    def test_unknown_diagnostic_or_stage_cannot_enter_result(self):
        for stage, status, code in (
            ("unknown", "passed", None),
            ("cleanup", "good", None),
            ("cleanup", "failed", "private://detail"),
            ("cleanup", "failed", "PRIVATE_IDENTIFIER_NOT_A_KNOWN_CODE"),
        ):
            with self.assertRaises(ValueError):
                RESULT.set_stage(self.result(), stage, status, code)
        with self.assertRaises(ValueError):
            RESULT.SmokeFailure("PRIVATE_IDENTIFIER_NOT_A_KNOWN_CODE")


class ClusterTests(unittest.TestCase):
    """Verify Spark 4.1.4 JSON field semantics with synthetic IDs only."""

    def payloads(self):
        return [
            {
                "url": RESULT.MASTER_URL,
                "status": "ALIVE",
                "workers": [
                    {
                        "id": "synthetic-worker",
                        "host": "spark-worker",
                        "port": 7078,
                        "state": "ALIVE",
                    }
                ],
            },
            {"id": "synthetic-worker", "masterurl": RESULT.MASTER_URL},
        ]

    def check(self, payloads):
        opener = mock.Mock()
        opener.open.side_effect = [Response(item) for item in payloads]
        result = RESULT.new_result({})
        RESULT.check_cluster(result, opener=opener)
        return result

    def test_master_and_registered_worker_are_both_required(self):
        result = self.check(self.payloads())
        self.assertEqual(
            result["stages"]["worker_registration"]["status"], "passed"
        )
        self.assertNotIn("synthetic-worker", json.dumps(result))

    def test_wrong_master_standby_dead_missing_or_mismatched_worker_fails(
        self,
    ):
        for fault in ("master", "standby", "empty", "dead", "id", "worker"):
            payloads = self.payloads()
            if fault == "master":
                payloads[0]["url"] = "spark://other:7077"
            elif fault == "standby":
                payloads[0]["status"] = "STANDBY"
            elif fault == "empty":
                payloads[0]["workers"] = []
            elif fault == "dead":
                payloads[0]["workers"][0]["state"] = "DEAD"
            elif fault == "id":
                payloads[1]["id"] = "not-registered"
            else:
                payloads[1]["masterurl"] = "spark://other:7077"
            with self.subTest(fault=fault):
                with self.assertRaises(RESULT.SmokeFailure):
                    self.check(payloads)

    def test_probe_transport_does_not_inherit_proxy_or_redirects(self):
        with mock.patch.object(
            RESULT.urllib.request, "build_opener"
        ) as factory:
            RESULT.internal_opener()
        handlers = factory.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(None))


class HttpTests(unittest.TestCase):
    """Exercise byte/page/row/time limits and fixed-origin cancellation."""

    def opener(self, *responses):
        opener = mock.Mock()
        opener.open.side_effect = list(responses)
        return opener

    def test_fixed_query_collects_two_pages_and_rejects_arbitrary_sql(self):
        opener = self.opener(
            Response({"data": SMOKE.EXPECTED[:1], "nextUri": NEXT}),
            Response({"data": SMOKE.EXPECTED[1:]}),
        )
        self.assertEqual(
            RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener), SMOKE.EXPECTED
        )
        request = opener.open.call_args_list[1].args[0]
        self.assertEqual(request.full_url, NEXT)
        with self.assertRaises(RESULT.SmokeFailure):
            RESULT.trino_rows("SELECT private", opener=opener)

    def test_cross_origin_userinfo_redirect_like_paths_are_never_requested(
        self,
    ):
        for url in (
            "http://external.example.invalid/v1/statement/executing/x/1",
            "http://user:secret@trino:8080/v1/statement/executing/x/1",
            "http://trino:8080/v1/statement/executing/../private",
            "http://trino:8080/v1/statement/executing/x/1?secret=x",
            "http://trino:8080/metrics",
            "http://trino:8080/v1/statement/executing/%2e%2e/private",
        ):
            opener = self.opener(Response({"nextUri": url}))
            with self.subTest(url=url):
                with self.assertRaises(RESULT.SmokeFailure):
                    RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener)
                self.assertEqual(opener.open.call_count, 1)

    def test_partial_failure_cancels_only_last_approved_page(self):
        opener = self.opener(
            Response({"nextUri": NEXT}),
            OSError("synthetic-private-detail"),
            Response({}, status=204),
        )
        with self.assertRaises(RESULT.SmokeFailure) as caught:
            RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener)
        self.assertEqual(caught.exception.cancellation, "succeeded")
        self.assertNotIn("synthetic-private", str(caught.exception))
        request = opener.open.call_args_list[-1].args[0]
        self.assertEqual(request.method, "DELETE")
        self.assertEqual(request.full_url, NEXT)

    def test_failed_cancellation_is_not_reported_as_success(self):
        opener = self.opener(
            Response({"nextUri": NEXT}),
            OSError("failed"),
            OSError("cancel failed"),
        )
        with self.assertRaises(RESULT.SmokeFailure) as caught:
            RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener)
        self.assertEqual(caught.exception.cancellation, "failed")

    def test_oversize_malformed_encoded_and_excess_rows_fail(self):
        for response in (
            Response(b"x" * (RESULT.MAX_BODY + 1), raw=True),
            Response(b"not-json", raw=True),
            Response({}, headers={"Content-Encoding": "gzip"}),
            Response({"data": SMOKE.EXPECTED + SMOKE.EXPECTED}),
        ):
            opener = self.opener(response)
            with self.assertRaises(RESULT.SmokeFailure):
                RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener)

    def test_page_limit_cancels_query_and_stops_polling(self):
        opener = self.opener(
            Response({"nextUri": NEXT}),
            Response({"nextUri": NEXT}),
            Response({}, status=204),
        )
        with mock.patch.object(RESULT, "MAX_PAGES", 2):
            with self.assertRaises(RESULT.SmokeFailure):
                RESULT.trino_rows(SMOKE.TRINO_SQL, opener=opener)
        self.assertEqual(opener.open.call_count, 3)

    def test_slow_dribble_is_checked_between_single_underlying_reads(self):
        response = Response({}, raw=False)
        response.read = mock.Mock(side_effect=AssertionError("buffered read"))
        response.read1 = mock.Mock(return_value=b" ")
        opener = self.opener(response)
        with mock.patch.object(
            RESULT.time, "monotonic", side_effect=[0, 0, 1, 2, 11]
        ):
            with self.assertRaises(RESULT.SmokeFailure):
                RESULT.read_json(opener, "fixed", 10)
        self.assertLessEqual(response.read1.call_count, 3)
        response.read.assert_not_called()


class SmokeRunTests(unittest.TestCase):
    """Prove stage ordering/failure semantics using a fake Spark boundary."""

    def setUp(self):
        self.events = []
        self.spark = Spark(self.events)
        self.blocked = False

    @contextlib.contextmanager
    def lock(self):
        self.events.append("lock-enter")

        def block():
            self.blocked = True

        try:
            yield block
        finally:
            self.events.append("lock-exit")

    def run_smoke(self, **changes):
        options = {
            "values": {},
            "session_factory": lambda: self.spark,
            "cluster_check": ready,
            "read_trino": lambda: copy.deepcopy(SMOKE.EXPECTED),
            "lock_factory": self.lock,
        }
        return SMOKE.run(**{**options, **changes})

    def test_full_success_reports_local_execution_and_replay_evidence(self):
        result = self.run_smoke()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(
            result["execution_mode"], "local[2]-with-cluster-readiness"
        )
        self.assertFalse(result["distributed_executors_verified"])
        self.assertEqual(set(result["counts"].values()), {3})
        self.assertTrue(result["value_match"])
        self.assertEqual(
            result["cleanup"]["artifact"], "retained_intentionally"
        )
        self.assertEqual(self.events.count(SMOKE.WRITE_SQL), 2)
        self.assertLess(
            self.events.index("stop"), self.events.index("lock-exit")
        )

    def test_local_only_skips_only_cluster_checks_and_cannot_use_remote_master(
        self,
    ):
        cluster = mock.Mock(side_effect=AssertionError)
        result = self.run_smoke(local_only=True, cluster_check=cluster)
        self.assertEqual(result["exit_code"], 0)
        cluster.assert_not_called()
        self.spark.sparkContext.master = "spark://spark-master:7077"
        result = self.run_smoke(local_only=True)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(self.events.count(SMOKE.WRITE_SQL), 2)

    def test_cluster_failure_prevents_session_or_lock_or_table_writes(self):
        session = mock.Mock()
        lock = mock.Mock()
        result = self.run_smoke(
            cluster_check=mock.Mock(
                side_effect=RESULT.SmokeFailure("WORKER_NOT_REGISTERED")
            ),
            session_factory=session,
            lock_factory=lock,
        )
        self.assertEqual(result["exit_code"], 1)
        session.assert_not_called()
        lock.assert_not_called()
        self.assertIsNone(result["counts"]["spark"])

    def test_partial_read_failure_stops_inside_lock_and_retains_known_artifact(
        self,
    ):
        result = self.run_smoke(
            read_trino=mock.Mock(side_effect=RuntimeError("private-driver"))
        )
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(
            result["cleanup"]["artifact"], "retained_intentionally"
        )
        self.assertNotIn("private-driver", json.dumps(result))
        self.assertLess(
            self.events.index("stop"), self.events.index("lock-exit")
        )

    def test_table_write_failure_reports_unknown_artifact_and_no_read_success(
        self,
    ):
        def fail_write(statement):
            if statement == SMOKE.WRITE_SQL:
                raise RuntimeError("private-storage-error")
            return Frame()

        self.spark.sql.side_effect = fail_write
        result = self.run_smoke()
        self.assertEqual(result["cleanup"]["artifact"], "unknown")
        self.assertEqual(
            result["stages"]["spark_readback"]["status"], "not_run"
        )
        self.assertNotIn("private-storage", json.dumps(result))

    def test_cleanup_failure_blocks_future_runs_and_never_becomes_success(
        self,
    ):
        self.spark.stop.side_effect = RuntimeError("private-stop-error")
        result = self.run_smoke()
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["cleanup"]["session_stopped"])
        self.assertTrue(self.blocked)
        self.assertEqual(result["stages"]["cleanup"]["status"], "failed")
        self.assertNotIn("private-stop", json.dumps(result))

    def test_failed_session_creation_cannot_assert_that_no_jvm_was_started(
        self,
    ):
        """A missing handle after getOrCreate failure is uncertain cleanup."""
        result = self.run_smoke(
            session_factory=mock.Mock(side_effect=RuntimeError("private"))
        )
        self.assertEqual(result["exit_code"], 1)
        self.assertFalse(result["cleanup"]["session_stopped"])
        self.assertTrue(self.blocked)
        self.assertEqual(
            result["stages"]["cleanup"]["code"], "SESSION_START_UNCONFIRMED"
        )

    def test_bool_id_float_decimal_and_unknown_write_target_are_rejected(self):
        self.assertFalse(SMOKE.expected_rows([[True, "x", "1.00"]]))
        frame = SimpleNamespace(
            take=lambda _: [SimpleNamespace(id=1, label="x", amount=1.0)]
        )
        with self.assertRaises(RESULT.SmokeFailure):
            SMOKE.collected_rows(frame)
        with self.assertRaises(RESULT.SmokeFailure):
            SMOKE.write_fixture(
                self.spark, "DROP TABLE iceberg.gold.production"
            )
        self.spark.sql.assert_not_called()

    def test_readable_sql_artifact_matches_fixed_allowlist_before_execution(
        self,
    ):
        SMOKE.validate_fixture_script()
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fixture.sql"
            script.write_text(SMOKE.NAMESPACE_SQL + "; DROP TABLE x;")
            with self.assertRaises(RESULT.SmokeFailure):
                SMOKE.validate_fixture_script(script)


@unittest.skipIf(
    os.geteuid() == 0, "Real lock tests require non-root temp UID"
)
class DisposableLinuxLockTests(unittest.TestCase):
    """Use only a fresh temporary directory owned by the running test UID."""

    def test_parallel_runs_are_exclusive_and_clean_release_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SMOKE.fixture_lock(root):
                with self.assertRaisesRegex(
                    RESULT.SmokeFailure, "SMOKE_ALREADY_RUNNING"
                ):
                    with SMOKE.fixture_lock(root):
                        self.fail("Second smoke acquired a held fixture lock.")
            with SMOKE.fixture_lock(root):
                pass
            self.assertEqual((root / SMOKE.LOCK_NAME).read_bytes(), b"")

    def test_block_marker_is_durable_and_preserves_other_work_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keep = root / "existing-evidence.txt"
            keep.write_bytes(b"retain exactly")
            with SMOKE.fixture_lock(root) as block:
                block()
            self.assertEqual(
                (root / SMOKE.LOCK_NAME).read_bytes(),
                b"RUN_IN_PROGRESS\n",
            )
            with self.assertRaisesRegex(
                RESULT.SmokeFailure, "SMOKE_PREVIOUS_SESSION_UNCONFIRMED"
            ):
                with SMOKE.fixture_lock(root):
                    self.fail("An uncertain previous session was ignored.")
            self.assertEqual(keep.read_bytes(), b"retain exactly")

    def test_abrupt_temporary_child_exit_leaves_durable_running_marker(self):
        """A disposable Python crash cannot silently unlock a future smoke."""
        with tempfile.TemporaryDirectory() as directory:
            code = (
                "import os,sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[1]); import spark_smoke; "
                "context=spark_smoke.fixture_lock(Path(sys.argv[2])); "
                "context.__enter__(); os._exit(7)"
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(ROOT / "infra/compute"),
                    directory,
                ],
                capture_output=True,
                timeout=5,
            )
            self.assertEqual(completed.returncode, 7)
            with self.assertRaisesRegex(
                RESULT.SmokeFailure, "SMOKE_PREVIOUS_SESSION_UNCONFIRMED"
            ):
                with SMOKE.fixture_lock(Path(directory)):
                    self.fail("An interrupted smoke was silently ignored.")

    def test_uncertain_stop_does_not_rewrite_or_truncate_its_durable_marker(
        self,
    ):
        """A write failure cannot create an empty uncertain-session marker."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SMOKE.fixture_lock(root) as block:
                with (
                    mock.patch.object(
                        SMOKE.os, "write", side_effect=OSError("disk fault")
                    ) as write,
                    mock.patch.object(
                        SMOKE.os,
                        "ftruncate",
                        side_effect=OSError("truncate fault"),
                    ) as truncate,
                ):
                    block()
                    write.assert_not_called()
                    truncate.assert_not_called()
            self.assertEqual(
                (root / SMOKE.LOCK_NAME).read_bytes(), b"RUN_IN_PROGRESS\n"
            )
            with self.assertRaisesRegex(
                RESULT.SmokeFailure, "SMOKE_PREVIOUS_SESSION_UNCONFIRMED"
            ):
                with SMOKE.fixture_lock(root):
                    self.fail("An uncertain session lost its refusal marker.")

    def test_symlink_root_or_leaf_and_hardlink_lock_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "original"
            original.mkdir()
            alias = root / "alias"
            alias.symlink_to(original, target_is_directory=True)
            with self.assertRaises(RESULT.SmokeFailure):
                with SMOKE.fixture_lock(alias):
                    self.fail("Symlink root accepted.")
            outside = root / "outside"
            outside.write_bytes(b"do not change")
            leaf = original / SMOKE.LOCK_NAME
            leaf.symlink_to(outside)
            with self.assertRaises(RESULT.SmokeFailure):
                with SMOKE.fixture_lock(original):
                    self.fail("Symlink lock accepted.")
            leaf.unlink()
            os.link(outside, leaf)
            with self.assertRaises(RESULT.SmokeFailure):
                with SMOKE.fixture_lock(original):
                    self.fail("Hardlinked lock accepted.")
            self.assertEqual(outside.read_bytes(), b"do not change")


if __name__ == "__main__":
    unittest.main()
