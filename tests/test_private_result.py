"""Private evidence I/O in disposable directories with synthetic owner views.

The sandbox maps filesystem root ownership. These tests explicitly model the
trusted-root/current-owner metadata; they are not host permission acceptance.
File creation, fsync, linking, no-follow and byte preservation use real tmpfs.
"""

import contextlib
import fcntl
import importlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
PRIVATE = importlib.import_module("private_result")


class PrivateResultTests(unittest.TestCase):
    """Never change an existing evidence file or follow a substituted path."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(
            prefix="private-evidence-unit-"
        )
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.record = {"run_id": "a" * 32, "overall_status": "failed"}
        real_fstat = os.fstat
        owner = self.path.stat().st_uid

        def synthetic_fstat(descriptor):
            """Model sticky root ownership for Linux contract tests."""
            metadata = real_fstat(descriptor)
            if stat.S_ISDIR(metadata.st_mode) and (
                metadata.st_mode & stat.S_ISVTX
            ):
                fields = list(metadata)
                fields[4] = 0
                return os.stat_result(fields)
            return metadata

        for patch in (
            mock.patch.object(PRIVATE.os, "fstat", synthetic_fstat),
            mock.patch.object(PRIVATE.os, "geteuid", return_value=owner),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_publish_is_atomic_private_and_existing_bytes_are_preserved(self):
        result = PRIVATE.write_result(self.path, self.record)
        target = self.path / result
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(target.read_bytes()), self.record)
        original = target.read_bytes()
        with self.assertRaises(PRIVATE.EvidenceError):
            PRIVATE.write_result(self.path, {**self.record, "new": True})
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(list(self.path.glob("*.tmp")), [])

    def test_restrictive_umask_does_not_break_exact_new_file_mode(self):
        previous = os.umask(0o777)
        try:
            name = PRIVATE.write_result(self.path, self.record)
        finally:
            os.umask(previous)
        self.assertEqual((self.path / name).stat().st_mode & 0o777, 0o600)

    def test_symlink_leaf_ancestor_and_result_name_are_refused(self):
        leaf = self.path / "alias"
        leaf.symlink_to(self.path, target_is_directory=True)
        for selected in (leaf, leaf / "other", self.path / "dangling"):
            if selected.name == "dangling":
                selected.symlink_to(
                    self.path / "missing", target_is_directory=True
                )
            with self.subTest(kind=selected.name):
                with self.assertRaises(PRIVATE.EvidenceError):
                    PRIVATE.validate_destination(selected)
        victim = self.path / "victim"
        victim.write_text("preserved")
        result_name = "spark-check-" + self.record["run_id"] + ".json"
        (self.path / result_name).symlink_to(victim)
        with self.assertRaises(PRIVATE.EvidenceError):
            PRIVATE.write_result(self.path, self.record)
        self.assertEqual(victim.read_text(), "preserved")

    def test_plan_has_no_write_probe_or_metadata_repair(self):
        with (
            mock.patch.object(
                PRIVATE.os, "fchmod", side_effect=AssertionError
            ),
            mock.patch.object(PRIVATE.os, "link", side_effect=AssertionError),
            mock.patch.object(
                PRIVATE.os, "unlink", side_effect=AssertionError
            ),
        ):
            PRIVATE.validate_destination(self.path)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_capacity_is_refused_without_deleting_prior_evidence(self):
        first = PRIVATE.write_result(self.path, self.record)
        original = (self.path / first).read_bytes()
        with mock.patch.object(PRIVATE, "MAX_RESULTS", 1):
            with self.assertRaises(PRIVATE.EvidenceError):
                PRIVATE.write_result(self.path, {"run_id": "b" * 32})
        self.assertEqual((self.path / first).read_bytes(), original)
        self.assertEqual(len(list(self.path.iterdir())), 1)

    def test_concurrent_publisher_is_refused_before_capacity_can_race(self):
        """Real advisory directory locks serialize the writer's threshold."""
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(PRIVATE.EvidenceError):
                PRIVATE.write_result(self.path, self.record)
        finally:
            os.close(descriptor)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_untrusted_mode_relative_and_checkout_targets_are_refused(self):
        self.path.chmod(0o770)  # Disposable fixture only.
        for path in (self.path, Path("relative"), ROOT, Path("/")):
            with self.assertRaises(PRIVATE.EvidenceError):
                PRIVATE.validate_destination(path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o770)

    def test_publication_and_cleanup_errors_are_normalized(self):
        with mock.patch.object(
            PRIVATE.os, "unlink", side_effect=OSError("unit-private-path")
        ):
            with self.assertRaises(PRIVATE.EvidenceError) as failure:
                PRIVATE.write_result(self.path, self.record)
        self.assertNotIn("unit-private-path", str(failure.exception))
        # Successful link may already exist; do not overwrite or delete it.
        target = self.path / ("spark-check-" + self.record["run_id"] + ".json")
        self.assertEqual(json.loads(target.read_text()), self.record)

    def test_replacement_is_observed_before_publishing(self):
        @contextlib.contextmanager
        def replaced_directory(path):
            del path
            descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                yield descriptor, mock.Mock(side_effect=PRIVATE.EvidenceError)
            finally:
                os.close(descriptor)

        with mock.patch.object(
            PRIVATE, "private_directory", replaced_directory
        ):
            with self.assertRaises(PRIVATE.EvidenceError):
                PRIVATE.write_result(self.path, self.record)
        self.assertEqual(list(self.path.iterdir()), [])

    def test_size_and_identity_rejected_before_creating_files(self):
        for result in (
            {"run_id": "../escape"},
            {"run_id": True},
            {"run_id": "b" * 32, "data": "x" * 65536},
        ):
            with self.assertRaises(PRIVATE.EvidenceError):
                PRIVATE.write_result(self.path, result)
        self.assertEqual(list(self.path.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
