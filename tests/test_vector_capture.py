"""Exercise finite log capture with fake Docker replies and temporary files.

No Docker API, real host logs, live credentials, permission changes or services
are used. The pipe tests execute only a bounded synthetic Python child.
"""

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location(
    "log_capture_subject", ROOT / "scripts/log_capture.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SITE_SPEC = importlib.util.spec_from_file_location(
    "vector_site_settings", ROOT / "scripts/settings.py"
)
SITE = importlib.util.module_from_spec(SITE_SPEC)
SITE_SPEC.loader.exec_module(SITE)


def settings(path, **changes):
    """Create synthetic selected settings without reading a private env."""
    return {
        "COMPOSE_PROFILES": "demo,monitoring,bi",
        "COMPOSE_PROJECT_NAME": "synthetic",
        "MONITORING_HOST_ALIAS": "lab",
        "MONITORING_LOG_DIR": str(path),
        "WORKER_UID": str(os.geteuid()),
        "WORKER_GID": str(os.getegid()),
        **changes,
    }


class FakeDocker:
    """Return invented labels and one finite stdout/stderr pair."""

    def __init__(self, *, labels=None, stdout=None, identifiers=None):
        self.calls = []
        self.identifiers = identifiers or ["a" * 64]
        self.labels = labels or {
            "com.docker.compose.project": "synthetic",
            "com.docker.compose.service": "worker",
            "com.docker.compose.container-number": "1",
            "app.owner": "lakehouse-foundation",
        }
        self.stdout = (
            b"2026-01-01T00:00:00.000000001Z INFO: synthetic\n"
            if stdout is None
            else stdout
        )

    def __call__(self, command, *, deadline):
        """Inspect command shape while avoiding any real Docker subprocess."""
        self.calls.append(command)
        self.assert_local(command)
        if "ps" in command:
            reply = ("\n".join(self.identifiers) + "\n").encode()
        elif "inspect" in command:
            reply = json.dumps(self.labels).encode()
        elif "logs" in command:
            return {
                "stdout": self.stdout,
                "stderr": b"2026-01-01T00:00:01Z INFO: synthetic stderr\n",
            }
        else:
            raise AssertionError("Unexpected fake command")
        return {"stdout": reply, "stderr": b""}

    @staticmethod
    def assert_local(command):
        """Prevent accidental remote or credential-rich CLI queries."""
        assert command[0:2] == ["docker", "--config"]
        assert command[3:5] == ["--context", "default"]
        assert ".Env" not in " ".join(command)


@unittest.skipUnless(
    os.name == "posix" and os.geteuid() != 0 and hasattr(os, "O_PATH"),
    "Disposable Linux tests require a non-root descriptor-capable platform.",
)
class CaptureTests(unittest.TestCase):
    """Use only private newly-created temporary spool directories."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="log-capture-unit-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.values = settings(self.path)

    def test_confirmation_precedes_all_docker_and_filesystem_queries(self):
        with mock.patch.object(MODULE, "walk", side_effect=AssertionError):
            for confirmation in (False, 1, "true"):
                runner = mock.Mock(side_effect=AssertionError)
                with self.assertRaisesRegex(
                    MODULE.CaptureError, "APPROVAL_REQUIRED"
                ):
                    MODULE.capture(
                        self.values, confirm=confirmation, runner=runner
                    )
                runner.assert_not_called()

    def test_generated_and_upgraded_settings_select_safe_default_host_alias(
        self,
    ):
        """Exercise actual configuration generation, not a hand-made alias."""
        with tempfile.TemporaryDirectory(prefix="vector-site-unit-") as root:
            path = Path(root) / "site.env"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(path),
            ]
            subprocess.run(command, check=True, capture_output=True)
            lines = path.read_text().splitlines()
            self.assertIn("MONITORING_HOST_ALIAS=local", lines)
            selected = []
            for line in lines:
                if line.startswith("COMPOSE_PROFILES="):
                    line = "COMPOSE_PROFILES=monitoring"
                elif line.startswith("MONITORING_LOG_DIR="):
                    line = "MONITORING_LOG_DIR=" + str(self.path)
                selected.append(line)
            path.write_text("\n".join(selected) + "\n")
            values = SITE.load(path)
            self.assertEqual(MODULE.selected(values)[1], "local")
            path.write_text(
                "\n".join(
                    line
                    for line in selected
                    if not line.startswith("MONITORING_HOST_ALIAS=")
                )
                + "\n"
            )
            subprocess.run(
                command + ["--upgrade"], check=True, capture_output=True
            )
            self.assertIn("MONITORING_HOST_ALIAS=local", path.read_text())
            before = SITE.load(path)
            self.assertEqual(MODULE.selected(before)[1], "local")
            for invalid in ("", "Actual.Host", "two words", "x" * 33):
                text = path.read_text().replace(
                    "MONITORING_HOST_ALIAS=local",
                    "MONITORING_HOST_ALIAS=" + invalid,
                )
                path.write_text(text)
                with self.assertRaises(ValueError):
                    SITE.load(path)
                path.write_text(
                    text.replace(
                        "MONITORING_HOST_ALIAS=" + invalid,
                        "MONITORING_HOST_ALIAS=local",
                    )
                )

    def test_selected_shape_and_owner_reject_without_docker(self):
        for changes in (
            {"COMPOSE_PROFILES": "demo"},
            {"COMPOSE_PROJECT_NAME": "UpperCase"},
            {"MONITORING_HOST_ALIAS": "invalid alias"},
            {"MONITORING_LOG_DIR": "/home/nonroot/.ssh/logs"},
            {"MONITORING_LOG_DIR": str(ROOT / "logs")},
            {"MONITORING_LOG_DIR": "/srv/../logs"},
            {"WORKER_UID": str(os.geteuid() + 1)},
            {"WORKER_GID": "group"},
        ):
            runner = mock.Mock(side_effect=AssertionError)
            with self.subTest(keys=list(changes)):
                with self.assertRaises(MODULE.CaptureError):
                    MODULE.capture(
                        settings(self.path, **changes),
                        confirm=True,
                        runner=runner,
                    )
                runner.assert_not_called()

    def test_capture_stream_metadata_private_modes_and_exact_replay(self):
        runner = FakeDocker()
        result = MODULE.capture(self.values, confirm=True, runner=runner)
        self.assertEqual(result["events"], 2)
        self.assertTrue(result["published"])
        self.assertFalse(result["exactly_once"])
        file = self.path / (result["file_id"] + ".jsonl")
        original = file.read_bytes()
        metadata = file.stat()
        self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
        records = [json.loads(line) for line in original.splitlines()]
        self.assertEqual([r["stream"] for r in records], ["stdout", "stderr"])
        self.assertEqual(records[0]["timestamp"].split(".")[1], "000000001Z")
        self.assertEqual(records[0]["container_alias"], "worker-1")
        self.assertNotIn("a" * 64, json.dumps(records))
        repeated = MODULE.capture(
            self.values, confirm=True, runner=FakeDocker()
        )
        self.assertFalse(repeated["published"])
        self.assertEqual(repeated["file_id"], result["file_id"])
        self.assertEqual(file.read_bytes(), original)
        self.assertEqual(file.stat().st_mtime_ns, metadata.st_mtime_ns)

    def test_foreign_project_owner_and_arbitrary_service_never_read_logs(self):
        for key, value in (
            ("com.docker.compose.project", "foreign"),
            ("app.owner", "unrelated"),
            ("com.docker.compose.service", "arbitrary"),
        ):
            runner = FakeDocker()
            runner.labels[key] = value
            result = MODULE.capture(self.values, confirm=True, runner=runner)
            self.assertEqual(result["events"], 0)
            self.assertFalse(any("logs" in call for call in runner.calls))
            self.assertEqual(list(self.path.iterdir()), [])

    def test_symlink_missing_and_unrelated_directory_refuse_before_docker(
        self,
    ):
        for kind in ("symlink", "missing", "unrelated"):
            with self.subTest(kind=kind):
                target = self.path / kind
                if kind == "symlink":
                    target.symlink_to(self.path, target_is_directory=True)
                elif kind == "unrelated":
                    target.mkdir(mode=0o700)
                    (target / "keep.txt").write_text("unchanged")
                runner = mock.Mock(side_effect=AssertionError)
                with self.assertRaises(ValueError):
                    MODULE.capture(
                        settings(target), confirm=True, runner=runner
                    )
                runner.assert_not_called()
        self.assertEqual(
            (self.path / "unrelated/keep.txt").read_text(), "unchanged"
        )

    def test_capture_limits_invalid_timestamp_and_replica_fail_closed(self):
        for runner in (
            FakeDocker(stdout=b"not-a-timestamp message\n"),
            FakeDocker(identifiers=["b" * 64] * 65),
        ):
            with self.assertRaises(MODULE.CaptureError):
                MODULE.capture(self.values, confirm=True, runner=runner)
            self.assertEqual(list(self.path.iterdir()), [])
        runner = FakeDocker()
        runner.labels["com.docker.compose.container-number"] = "unexpected"
        with self.assertRaisesRegex(MODULE.CaptureError, "REPLICA_INVALID"):
            MODULE.capture(self.values, confirm=True, runner=runner)

    def test_truncation_uses_original_bytes_for_stable_position(self):
        text = b"x" * (MODULE.MAX_MESSAGE_BYTES + 1)
        output = {"stdout": b"2026-01-01T00:00:00Z " + text, "stderr": b""}
        record = list(
            MODULE.capture_lines(
                output,
                project="synthetic",
                alias="lab",
                identity="b" * 64,
                service="worker",
                replica="1",
            )
        )[0]
        self.assertTrue(record["truncated"])
        self.assertEqual(len(record["message"]), MODULE.MAX_MESSAGE_BYTES)
        self.assertEqual(
            record["source_position"],
            hashlib.sha256(b"2026-01-01T00:00:00Z|stdout|" + text).hexdigest(),
        )

    def test_capacity_has_no_automatic_deletion(self):
        for index in range(MODULE.MAX_FILES):
            target = self.path / (f"{index:064x}" + ".jsonl")
            target.write_bytes(b"")
            target.chmod(0o600)
        runner = mock.Mock(side_effect=AssertionError)
        with self.assertRaisesRegex(MODULE.CaptureError, "SPOOL_CAPACITY"):
            MODULE.capture(self.values, confirm=True, runner=runner)
        runner.assert_not_called()
        self.assertEqual(len(list(self.path.iterdir())), MODULE.MAX_FILES)

    def test_bounds_rejected_before_queries_and_payload_never_published(self):
        for options in ({"tail": 0}, {"minutes": 1441}, {"tail": True}):
            with self.assertRaises(MODULE.CaptureError):
                MODULE.capture(self.values, confirm=True, **options)
        with mock.patch.object(MODULE, "MAX_CAPTURE_BYTES", 1):
            with self.assertRaisesRegex(MODULE.CaptureError, "BYTE_LIMIT"):
                MODULE.capture(self.values, confirm=True, runner=FakeDocker())
        self.assertEqual(list(self.path.iterdir()), [])


class BoundedProcessTests(unittest.TestCase):
    """Read only invented stdout/stderr from an unprivileged Python child."""

    def test_both_pipes_and_nonzero_error_are_bounded_and_normalized(self):
        reply = MODULE.bounded_command(
            [
                sys.executable,
                "-c",
                "import sys;print('unit');sys.stderr.write('unit')",
            ],
            deadline=time.monotonic() + 5,
        )
        self.assertEqual(reply, {"stdout": b"unit\n", "stderr": b"unit"})
        with self.assertRaisesRegex(MODULE.CaptureError, "DOCKER_REJECTED"):
            MODULE.bounded_command(
                [sys.executable, "-c", "raise SystemExit(2)"],
                deadline=time.monotonic() + 5,
            )

    def test_byte_limit_deadline_and_child_environment(self):
        with mock.patch.object(MODULE, "MAX_COMMAND_BYTES", 8):
            with self.assertRaisesRegex(MODULE.CaptureError, "COMMAND_LIMIT"):
                MODULE.bounded_command(
                    [sys.executable, "-c", "print('x'*16)"],
                    deadline=time.monotonic() + 5,
                )
        with self.assertRaisesRegex(MODULE.CaptureError, "DEADLINE"):
            MODULE.bounded_command(
                [sys.executable, "-c", "import time;time.sleep(1)"],
                deadline=time.monotonic() + 0.05,
            )
        with mock.patch.dict(os.environ, {"SYNTHETIC_PRIVATE": "unit-value"}):
            reply = MODULE.bounded_command(
                [sys.executable, "-c", "import os;print(sorted(os.environ))"],
                deadline=time.monotonic() + 5,
            )
        self.assertNotIn(b"SYNTHETIC_PRIVATE", reply["stdout"])
