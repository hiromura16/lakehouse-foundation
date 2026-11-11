"""Synthetic orchestration contracts; no Docker daemon or smoke writes.

The one bounded subprocess test launches only Python printing fixture text.
Docker labels, health, volumes and image identity are mocked observations.
"""

import contextlib
import importlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
CHECK = importlib.import_module("compute_check")
SETTINGS = importlib.import_module("settings")


class SyntheticDocker:
    """Model explicit metadata and outcomes without contacting a daemon."""

    def __init__(self, values):
        self.values = values
        self.calls = []
        self.missing = set()
        self.unhealthy = set()
        self.foreign = set()
        self.mount_prefix = (
            values.get("DATA_VOLUME_PREFIX") or values["COMPOSE_PROJECT_NAME"]
        )
        self.build_fail = False
        self.timeout = False
        self.extra = {}
        self.ids = {}
        self.run_id = None
        self.child_mutation = lambda value: value

    def __call__(self, command, env, **options):
        self.calls.append((list(command), dict(env), dict(options)))
        okay = CHECK.CommandResult
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return okay(0, ("a" * 40).encode())
        if command[:3] == ["git", "status", "--porcelain"]:
            return okay(0)
        if command[:3] == ["docker", "image", "inspect"]:
            return okay(0, ("sha256:" + "b" * 64).encode())
        if command[:2] == ["docker", "stop"]:
            return okay(0)
        if command[:2] == ["docker", "inspect"]:
            target = command[-1]
            if command[3] == "{{.Id}}":
                identifier = "c" * 64
                self.ids[identifier] = "spark-check"
                return okay(0, identifier.encode())
            name = self.ids[target]
            project = self.values["COMPOSE_PROJECT_NAME"]
            item = {
                "owner": "lakehouse-foundation",
                "service": name,
                "project": project
                if name not in self.foreign
                else "unrelated",
                "run_id": self.run_id,
                "state": "exited"
                if name.endswith(("init", "migrate"))
                else "running",
                "health": "unhealthy" if name in self.unhealthy else "healthy",
                "exit_code": 0,
                "mounts": [
                    {
                        "type": "volume",
                        "name": self.mount_prefix + "_" + volume,
                        "target": "/opt/spark/" + directory,
                    }
                    for volume, directory in (
                        ("spark-events", "events"),
                        ("spark-work", "work"),
                    )
                ],
            }
            return okay(0, json.dumps(item).encode())
        if "ps" in command:
            name = command[-1]
            if name in self.missing:
                return okay(0)
            identifier = format(len(self.ids) + 1, "064x")
            self.ids[identifier] = name
            return okay(0, identifier.encode())
        if "build" in command:
            return okay(1 if self.build_fail else 0)
        if "run" in command:
            variables = {}
            for index, part in enumerate(command[:-1]):
                if part == "--env":
                    key, value = command[index + 1].split("=", 1)
                    variables[key] = value
            self.run_id = variables["SPARK_SMOKE_RUN_ID"]
            if self.timeout:
                return okay(-1, error="COMMAND_TIMEOUT")
            result = CHECK.CONTRACT.new_result(
                variables, local_only="--local-only" in command
            )
            for name in CHECK.CONTRACT.STAGES:
                skipped = "--local-only" in command and name in {
                    "master_reachability",
                    "worker_registration",
                }
                CHECK.CONTRACT.set_stage(
                    result, name, "skipped" if skipped else "passed"
                )
            result["counts"] = {key: 3 for key in result["counts"]}
            result["value_match"] = True
            result["cleanup"].update(
                artifact="retained_intentionally", session_stopped=True
            )
            result["versions"]["spark"] = "4.1.4"
            result.update(self.extra)
            self.child_mutation(result)
            line = CHECK.CONTRACT.result_record(result).encode()
            return okay(0, records=(line,))
        raise AssertionError("Unexpected synthetic command category.")


class ComputeOrchestrationTests(unittest.TestCase):
    """Prove targeting and evidence flow, not runtime interoperability."""

    def setUp(self):
        self.values = {
            "COMPOSE_PROJECT_NAME": "selected-app",
            "DATA_VOLUME_PREFIX": "",
            "COMPOSE_PROFILES": "compute,notebooks",
        }
        self.docker = SyntheticDocker(self.values)
        self.compose = ["docker", "compose", "--project-name", "selected-app"]

    def run_check(self, **options):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = CHECK.run_check(
                self.compose, self.values, {}, runner=self.docker, **options
            )
        line = output.getvalue().strip()
        self.assertTrue(line.startswith(CHECK.CONTRACT.RESULT_PREFIX))
        result = json.loads(line.removeprefix(CHECK.CONTRACT.RESULT_PREFIX))
        return status, result, output.getvalue()

    def test_success_has_all_stages_and_owner_build_before_run(self):
        status, result, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(result["counts"]["replay_trino"], 3)
        self.assertFalse(result["distributed_executors_verified"])
        self.assertEqual(result["image_identity"], "unknown")
        self.assertEqual(result["built_image_identity"], "sha256:" + "b" * 64)
        commands = [call[0] for call in self.docker.calls]
        build = next(
            index for index, cmd in enumerate(commands) if "build" in cmd
        )
        run = next(index for index, cmd in enumerate(commands) if "run" in cmd)
        self.assertLess(build, run)
        self.assertEqual(commands[build][-1], "spark-volumes-init")
        self.assertNotIn("up", sum(commands, []))
        self.assertNotIn("down", sum(commands, []))
        self.assertIn("--rm", commands[run])
        self.assertIn("--no-deps", commands[run])
        self.assertNotIn("--pull", commands[run])

    def test_missing_or_unhealthy_core_and_workers_fail_before_build(self):
        for name in (
            "worker",
            "trino",
            "catalog",
            "catalog-db",
            "storage",
            "spark-master",
            "spark-worker",
            "catalog-migrate",
        ):
            with self.subTest(service=name):
                self.docker = SyntheticDocker(self.values)
                self.docker.missing.add(name)
                status, result, _ = self.run_check()
                self.assertEqual(status, 1)
                self.assertFalse(
                    any("build" in call[0] for call in self.docker.calls)
                )
                self.assertFalse(
                    any("run" in call[0] for call in self.docker.calls)
                )
                self.assertEqual(
                    result["stages"]["session_creation"]["status"], "not_run"
                )

    def test_target_label_or_volume_mismatch_is_never_repaired(self):
        for failure in ("project", "volume"):
            self.docker = SyntheticDocker(self.values)
            if failure == "project":
                self.docker.foreign.add("worker")
            else:
                self.docker.mount_prefix = "wrong-prefix"
            status, _, _ = self.run_check()
            self.assertEqual(status, 1)
            self.assertFalse(
                any("build" in call[0] for call in self.docker.calls)
            )

    def test_explicit_data_prefix_is_checked_without_recreating_volumes(self):
        self.values["DATA_VOLUME_PREFIX"] = "kept-data"
        self.docker = SyntheticDocker(self.values)
        self.assertEqual(self.run_check()[0], 0)
        self.assertFalse(
            any("volume" in call[0][:3] for call in self.docker.calls)
        )

    def test_local_only_is_labeled_and_does_not_require_daemons(self):
        self.docker.missing.update({"spark-master", "spark-worker"})
        status, result, _ = self.run_check(local_only=True)
        self.assertEqual(status, 0)
        self.assertEqual(result["execution_mode"], "local[2]-only")
        self.assertEqual(
            result["stages"]["master_reachability"]["status"], "skipped"
        )

    def test_build_failure_still_has_json_and_no_container_run(self):
        self.docker.build_fail = True
        status, result, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(result["host_stages"]["build"]["status"], "failed")
        self.assertFalse(any("run" in call[0] for call in self.docker.calls))

    def test_unknown_fields_are_removed_and_malformed_counts_fail(self):
        self.docker.extra = {
            "password": "unit-secret",
            "raw_log": "private-text",
        }
        status, _, output = self.run_check()
        self.assertEqual(status, 0)
        self.assertNotIn("unit-secret", output)
        self.assertNotIn("private-text", output)
        self.docker.child_mutation = lambda value: value["counts"].update(
            spark=True
        )
        status, _, _ = self.run_check()
        self.assertEqual(status, 1)

    def test_timeout_stops_only_our_inspected_one_shot_and_stays_failed(self):
        self.docker.timeout = True
        status, result, _ = self.run_check()
        self.assertEqual(status, 1)
        self.assertEqual(result["container_cleanup"], "stopped_owned_one_shot")
        stops = [
            call[0]
            for call in self.docker.calls
            if call[0][:2] == ["docker", "stop"]
        ]
        self.assertEqual(stops, [["docker", "stop", "--time", "10", "c" * 64]])
        self.docker.foreign.add("spark-check")
        self.docker.calls.clear()
        self.assertEqual(self.run_check()[1]["container_cleanup"], "unknown")
        self.assertFalse(
            any(
                call[0][:2] == ["docker", "stop"] for call in self.docker.calls
            )
        )

    def test_selected_project_replaces_shell_but_preserves_private_bypass(
        self,
    ):
        values = {**self.values, "NO_PROXY": "private.example.invalid"}
        selected = CHECK.selected_project(values, "actual-app")
        with mock.patch.dict(
            os.environ, {"COMPOSE_PROJECT_NAME": "stale-app"}
        ):
            env = SETTINGS.environment(selected)
        self.assertEqual(env["COMPOSE_PROJECT_NAME"], "actual-app")
        self.assertIn("private.example.invalid", env["NO_PROXY"])
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], "selected-app")
        for invalid in ("", "../invalid", "UPPERCASE", "with space"):
            with self.assertRaises(ValueError):
                CHECK.selected_project(values, invalid)

    def test_noisy_subprocess_output_keeps_one_machine_record(self):
        tag = CHECK.CONTRACT.RESULT_PREFIX + '{"unit":true}'
        code = 'print("synthetic JVM progress"); print(' + repr(tag) + ")"
        result = CHECK.bounded_command(
            [sys.executable, "-c", code], {}, timeout=5, capture=False
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.records, (tag.encode(),))
        self.assertEqual(result.output, b"")

    def test_unavailable_settings_emit_failure_without_raw_error(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(CHECK.settings_failure(), 1)
        value = json.loads(output.getvalue().strip().split("=", 1)[1])
        self.assertEqual(
            value["host_stages"]["settings"]["code"], "SETTINGS_INVALID"
        )
        self.assertEqual(value["counts"]["spark"], None)


if __name__ == "__main__":
    unittest.main()
