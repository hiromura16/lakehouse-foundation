"""Validate the opt-in integration harness as source without Docker/sockets.

These tests exercise configuration generation, isolation, bounded evidence and
cleanup with synthetic temporary files or mocked subprocess/HTTP replies. They
are not actual Vector/Loki/OpenObserve or saturation results.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "infra/monitoring/vector/tests/integration"


def load(name):
    """Import public harness code without invoking its main function."""
    spec = importlib.util.spec_from_file_location(
        name, DIRECTORY / (name + ".py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INVOKE = load("invoke")
PROBE = load("probe")


class HarnessPlanTests(unittest.TestCase):
    """Keep the default path entirely separate from authorized runtime."""

    def test_default_plan_uses_no_docker_or_private_settings(self):
        output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["invoke"]),
            mock.patch.object(
                INVOKE.subprocess, "run", side_effect=AssertionError
            ),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(INVOKE.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "plan")
        self.assertEqual(result["network"], "none")
        self.assertEqual(result["host_mounts"], [])
        self.assertEqual(result["production_disk_saturation"], "not_run")
        self.assertIn("stub", result["actual_openobserve"])

    def test_explicit_command_isolation_and_unsafe_names(self):
        build, run = INVOKE.commands(
            "/tmp/unit-cli",
            "lakehouse-vector-isolated:" + "a" * 16,
            "/tmp/unit-public",
            "lakehouse-vector-trial-" + "b" * 16,
        )
        self.assertIn("/tmp/unit-public", build)
        self.assertNotIn(str(ROOT), build)
        for flag, value in (
            ("--network", "none"),
            ("--cap-drop", "ALL"),
            ("--user", "10001:10001"),
            ("--memory", "1g"),
            ("--pids-limit", "128"),
            ("--ipc", "none"),
        ):
            self.assertEqual(run[run.index(flag) + 1], value)
        self.assertIn("--read-only", run)
        for denied in ("--privileged", "--mount", "--volume", "-v", "-p"):
            self.assertNotIn(denied, run)
        for image, name in (
            ("application:latest", "lakehouse-vector-trial-" + "b" * 16),
            ("lakehouse-vector-isolated:" + "a" * 16, "existing-service"),
        ):
            with self.assertRaises(ValueError):
                INVOKE.commands("/tmp/unit-cli", image, "/tmp/unit", name)

    def test_context_is_fixed_public_files_without_private_checkout_content(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="vector-public-unit-") as root:
            target = Path(root)
            INVOKE.public_context(target)
            files = [
                p.relative_to(target) for p in target.rglob("*") if p.is_file()
            ]
            self.assertEqual(len(files), 10)
            self.assertIn(Path("Dockerfile"), files)
            self.assertIn(
                Path("infra/monitoring/vector/tests/integration/probe.py"),
                files,
            )
            self.assertFalse(any(".env" in str(path) for path in files))
            for path in files:
                self.assertFalse((target / path).is_symlink())

    def test_runtime_mock_bounds_environment_and_cleans_exact_random_name(
        self,
    ):
        calls = []

        def fake(command, **options):
            calls.append(command)
            self.assertEqual(options["env"], {"PATH": os.defpath})
            if "run" in command:
                options["stdout"].write(
                    json.dumps(
                        {"schema_version": 1, "status": "passed"}
                    ).encode()
                )
            return SimpleNamespace(returncode=0)

        with mock.patch.object(INVOKE.subprocess, "run", side_effect=fake):
            result = INVOKE.run_trial()
        self.assertEqual(result["container_exit_code"], 0)
        self.assertEqual(len(calls), 3)
        run = calls[1]
        cleanup = calls[2]
        name = run[run.index("--name") + 1]
        self.assertEqual(cleanup[-3:], ["rm", "--force", name])
        self.assertTrue(name.startswith("lakehouse-vector-trial-"))

    def test_ambiguous_cleanup_failure_is_not_reported_as_absence(self):
        def fake(command, **options):
            if "run" in command:
                options["stdout"].write(
                    b'{"schema_version":1,"status":"passed"}'
                )
            if "rm" in command or "ls" in command:
                return SimpleNamespace(returncode=1, stdout=b"")
            return SimpleNamespace(returncode=0)

        with mock.patch.object(INVOKE.subprocess, "run", side_effect=fake):
            with self.assertRaisesRegex(ValueError, "CLEANUP_FAILED"):
                INVOKE.run_trial()

    def test_nonzero_exit_or_failed_receipt_cannot_be_reported_as_success(
        self,
    ):
        for exit_code, status in ((7, "passed"), (0, "failed")):
            calls = []

            def fake(command, **options):
                calls.append(command)
                if "run" in command:
                    options["stdout"].write(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "status": status,
                            }
                        ).encode()
                    )
                    return SimpleNamespace(returncode=exit_code)
                return SimpleNamespace(returncode=0)

            output = io.StringIO()
            with (
                mock.patch.object(INVOKE.subprocess, "run", side_effect=fake),
                mock.patch.object(sys, "argv", ["invoke", "--run"]),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(INVOKE.main(), 1)
            result = json.loads(output.getvalue())
            self.assertNotEqual(result["status"], "passed")
            self.assertEqual(result["container_exit_code"], exit_code)
            self.assertTrue(any("rm" in command for command in calls))

    def test_binary_stages_are_exact_pins_without_package_or_rust_build(self):
        text = (DIRECTORY / "Dockerfile").read_text()
        for key in ("vector", "loki", "python"):
            lock = json.loads(
                (ROOT / "infra/lakehouse/images.lock.json").read_text()
            )[key]["pinned"]
            self.assertIn("FROM " + lock, text)
        for denied in ("cargo build", "apt-get", "pip install", "docker.sock"):
            self.assertNotIn(denied, text)


class HarnessVariantTests(unittest.TestCase):
    """Inspect only generated configuration and finite synthetic data."""

    def test_memory_variant_is_explicit_and_keeps_shared_redaction(self):
        with tempfile.TemporaryDirectory(
            prefix="vector-variant-unit-"
        ) as root:
            directory = Path(root)
            with (
                mock.patch.object(
                    PROBE, "CONFIG", ROOT / "infra/monitoring/vector"
                ),
                mock.patch.object(
                    PROBE,
                    "LOKI_CONFIG",
                    ROOT / "infra/monitoring/loki/loki.yaml",
                ),
            ):
                selected = PROBE.variants(directory)
            common = yaml.safe_load((selected / "common.yaml").read_text())
            self.assertEqual(common["data_dir"], str(directory / "state"))
            self.assertEqual(
                common["transforms"]["redacted"]["inputs"], ["multiline"]
            )
            for filename in ("loki.yaml", "openobserve.yaml"):
                config = yaml.safe_load((selected / filename).read_text())
                sink = config["sinks"][filename.removesuffix(".yaml")]
                self.assertEqual(sink["buffer"]["type"], "memory")
                self.assertEqual(sink["buffer"]["max_events"], 8)
                self.assertEqual(sink["buffer"]["when_full"], "drop_newest")
            loki = yaml.safe_load((directory / "loki.yaml").read_text())
            self.assertEqual(
                loki["server"]["http_listen_address"], "127.0.0.1"
            )
            self.assertEqual(
                loki["server"]["grpc_listen_address"], "127.0.0.1"
            )
            self.assertEqual(
                (directory / "secret").stat().st_mode & 0o777, 0o600
            )

    def test_identity_leak_and_level_contracts_do_not_infer_missing_evidence(
        self,
    ):
        record = {
            "source_event_id": "a" * 64,
            "level": "unknown",
            "level_normalized": "unknown",
            "message": "synthetic trial event 1",
        }
        self.assertEqual(PROBE.ids([record]), {"a" * 64})
        self.assertTrue(PROBE.has_phase([record], "trial"))
        self.assertFalse(PROBE.has_phase([record], "recovery"))
        for changes in (
            {"source_event_id": "bad"},
            {"level": "error"},
            {"message": "unit-redact-marker"},
        ):
            with self.assertRaises(PROBE.TestFailure):
                PROBE.ids([{**record, **changes}])

    def test_atomic_synthetic_publication_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory(prefix="vector-emit-unit-") as root:
            directory = Path(root)
            rows = [{"message": "synthetic"}]
            PROBE.publish(directory, rows)
            original = next(directory.glob("*.jsonl"))
            content = original.read_bytes()
            with self.assertRaises(FileExistsError):
                PROBE.publish(directory, rows)
            self.assertEqual(original.read_bytes(), content)

    def test_real_metric_name_is_observed_not_assumed(self):
        trial = PROBE.Trial.__new__(PROBE.Trial)
        # Invented protocol fixture: only tests extraction/filtering. It is
        # not evidence that this metric exists in the selected Vector image.
        sample = (
            'synthetic_discarded_events_total{component_id="openobserve"} 3\n'
            'synthetic_discarded_events_total{component_id="loki"} 2\n'
        ).encode()
        with mock.patch.object(PROBE, "request", return_value=sample):
            result = trial.drop_evidence("openobserve")
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["names"], ["synthetic_discarded_events_total"])
        with mock.patch.object(PROBE, "request", return_value=b""):
            self.assertEqual(trial.drop_evidence("loki")["count"], 0)

    def test_child_cleanup_is_bounded(self):
        child = mock.Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("unit", 10), 0]
        with self.assertRaisesRegex(PROBE.TestFailure, "CLEANUP_TIMEOUT"):
            PROBE.terminate(child)
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_args_list[-1], mock.call(timeout=2))
