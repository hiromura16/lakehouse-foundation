"""Check Spark readiness and safe volume initialisation without Docker."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "spark_cluster_subject", ROOT / "infra/compute/spark_cluster.py"
)
CLUSTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLUSTER)


class SparkVolumeTests(unittest.TestCase):
    """Changing dedicated mount roots must never walk through stored data."""

    def test_initialisation_retains_files_and_changes_only_mount_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ("events", "work")]
            for root in roots:
                root.mkdir()
                (root / "keep.txt").write_text("existing evidence")
            with mock.patch.object(CLUSTER.os, "chown") as chown:
                CLUSTER.initialise_volumes("10001", "10002", roots)
                CLUSTER.initialise_volumes("10001", "10002", roots)
            self.assertEqual(chown.call_count, 4)
            self.assertEqual(
                {call.args[0] for call in chown.call_args_list}, set(roots)
            )
            for call in chown.call_args_list:
                self.assertFalse(call.kwargs["follow_symlinks"])
            for root in roots:
                self.assertEqual(root.stat().st_mode & 0o7777, 0o2770)
                self.assertEqual(
                    (root / "keep.txt").read_text(), "existing evidence"
                )

    def test_symlink_root_is_rejected_before_changing_any_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "events"
            first.mkdir()
            second = root / "work"
            second.symlink_to(first, target_is_directory=True)
            with mock.patch.object(CLUSTER.os, "chown") as chown:
                with self.assertRaises(ValueError):
                    CLUSTER.initialise_volumes(
                        "10001", "10002", [first, second]
                    )
            chown.assert_not_called()

    def test_invalid_runtime_identity_is_rejected_before_filesystem_access(
        self,
    ):
        for uid, gid in [("0", "1"), ("", "1"), ("1", "-1"), ("abc", "2")]:
            with self.subTest(uid=uid, gid=gid):
                with self.assertRaises(ValueError):
                    CLUSTER.initialise_volumes(uid, gid)


class SparkReadinessTests(unittest.TestCase):
    """An active web server must also expose the expected Spark state."""

    def readiness(self, role, payload):
        """Feed a bounded JSON response through the real readiness logic."""
        response = io.BytesIO(json.dumps(payload).encode())
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            CLUSTER.urllib.request, "build_opener", return_value=opener
        ) as build:
            result = CLUSTER.healthy(role)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(
            opener.open.call_args.args, (CLUSTER.HEALTH_URLS[role],)
        )
        return result

    def test_master_must_be_alive(self):
        self.assertTrue(self.readiness("master", {"status": "ALIVE"}))
        self.assertFalse(self.readiness("master", {"status": "STANDBY"}))

    def test_worker_must_name_the_expected_registered_master(self):
        self.assertTrue(
            self.readiness(
                "worker", {"masterurl": "spark://spark-master:7077"}
            )
        )
        self.assertFalse(self.readiness("worker", {"masterurl": ""}))

    def test_empty_history_is_ready_but_a_wrong_shape_is_not(self):
        self.assertTrue(self.readiness("history", []))
        self.assertFalse(self.readiness("history", {"error": "not history"}))

    def test_transient_connection_error_is_unhealthy(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError("synthetic unavailable endpoint")
        with mock.patch.object(
            CLUSTER.urllib.request, "build_opener", return_value=opener
        ):
            self.assertFalse(CLUSTER.healthy("master"))


if __name__ == "__main__":
    unittest.main()
