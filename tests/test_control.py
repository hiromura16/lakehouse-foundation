"""Check private environment selection without Docker or network access."""

import contextlib
import importlib.util
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts/control.py"
CONFIGURE = ROOT / "scripts/configure.py"
SPEC = importlib.util.spec_from_file_location(
    "control_settings_subject", ROOT / "scripts/settings.py"
)
SETTINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETTINGS)


class ControlFixture:
    """Provide synthetic settings and an entirely mocked CLI subprocess."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="control-settings-")
        self.addCleanup(temporary.cleanup)
        self.env_file = Path(temporary.name) / "private/site.env"
        # Generate realistic settings, including random private credentials.
        with (
            mock.patch.dict(sys.modules, {"settings": SETTINGS}),
            mock.patch.object(
                sys,
                "argv",
                [str(CONFIGURE), "--output", str(self.env_file)],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(CONFIGURE), run_name="__main__")

    def invoke(self, arguments, fallback=None):
        """Run the CLI while recording every external process request."""
        environment = dict(os.environ)
        environment.pop("LAKEHOUSE_ENV_FILE", None)
        if fallback is not None:
            environment["LAKEHOUSE_ENV_FILE"] = fallback
        output = io.StringIO()
        errors = io.StringIO()
        exit_code = None
        with (
            mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]),
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.dict(sys.modules, {"settings": SETTINGS}),
            mock.patch.object(sys, "argv", [str(CONTROL), *arguments]),
            mock.patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as process,
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("No test network access allowed."),
            ),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(errors),
        ):
            try:
                runpy.run_path(str(CONTROL), run_name="__main__")
            except SystemExit as error:
                exit_code = error.code
        return exit_code, process, output.getvalue(), errors.getvalue()


class ControlEnvironmentTests(ControlFixture, unittest.TestCase):
    """Select the intended file and retain the real private-file validator."""

    def test_explicit_file_takes_precedence_over_environment(self):
        exit_code, process, output, _ = self.invoke(
            ["--env-file", str(self.env_file), "plan"],
            fallback=str(self.env_file.parent / "missing-fallback.env"),
        )
        self.assertIsNone(exit_code)
        command = process.call_args.args[0]
        self.assertEqual(
            command[command.index("--env-file") + 1],
            str(self.env_file.resolve()),
        )
        self.assertEqual(
            output, "Compose configuration valid. Profiles: core only\n"
        )

    def test_spark_target_overrides_project_interpolation_only_explicitly(
        self,
    ):
        """A stale shell project cannot select another validation target."""
        with mock.patch.object(
            sys, "path", [str(ROOT / "scripts"), *sys.path]
        ):
            import compute_check
        with (
            mock.patch.object(
                compute_check, "run_check", return_value=0
            ) as run,
            mock.patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": "stale-app"}),
        ):
            code, process, _, _ = self.invoke(
                [
                    "--env-file",
                    str(self.env_file),
                    "--project-name",
                    "selected-app",
                    "spark-check",
                ]
            )
        self.assertEqual(code, 0)
        process.assert_not_called()
        command, values, environment = run.call_args.args
        self.assertEqual(
            command[command.index("--project-name") + 1], "selected-app"
        )
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], "selected-app")
        self.assertEqual(environment["COMPOSE_PROJECT_NAME"], "selected-app")
        self.assertEqual(environment["NO_PROXY"], environment["no_proxy"])

    def test_workspace_profile_off_apply_probe_is_a_nonmutating_noop(self):
        """Explicit preparation cannot require an inactive notebook path."""
        with mock.patch.object(
            sys, "path", [str(ROOT / "scripts"), *sys.path]
        ):
            code, process, output, _ = self.invoke(
                [
                    "--env-file",
                    str(self.env_file),
                    "--apply",
                    "--probe",
                    "notebook-workspace",
                ]
            )
        self.assertIsNone(code)
        self.assertFalse(json.loads(output)["enabled"])
        process.assert_not_called()

    def test_narrow_validation_flags_cannot_modify_other_lifecycle_actions(
        self,
    ):
        for flags in (
            ["--project-name", "another-app"],
            ["--apply"],
            ["--probe"],
            ["--local-only"],
        ):
            with self.subTest(flags=flags):
                code, process, _, _ = self.invoke(
                    ["--env-file", str(self.env_file), *flags, "plan"]
                )
                self.assertEqual(code, 2)
                process.assert_not_called()

    def test_environment_fallback_runs_through_the_private_validator(self):
        exit_code, process, output, _ = self.invoke(
            ["plan"], fallback=str(self.env_file)
        )
        self.assertIsNone(exit_code)
        command = process.call_args.args[0]
        self.assertEqual(command[:2], ["docker", "compose"])
        self.assertEqual(
            command[command.index("--env-file") + 1],
            str(self.env_file.resolve()),
        )
        self.assertEqual(command[-2:], ["config", "--quiet"])
        self.assertIn("Compose configuration valid", output)
        process.assert_called_once()

    def test_missing_or_empty_selection_is_a_clear_argument_error(self):
        for fallback in [None, ""]:
            with self.subTest(fallback=fallback):
                exit_code, process, _, errors = self.invoke(
                    ["plan"], fallback=fallback
                )
                self.assertEqual(exit_code, 2)
                self.assertIn(
                    "Provide --env-file or set LAKEHOUSE_ENV_FILE.", errors
                )
                process.assert_not_called()

    def test_environment_fallback_does_not_bypass_file_permissions(self):
        self.env_file.chmod(0o644)
        exit_code, process, _, _ = self.invoke(
            ["plan"], fallback=str(self.env_file)
        )
        self.assertIn("0600", exit_code)
        process.assert_not_called()

    def test_environment_fallback_cannot_select_a_repository_file(self):
        exit_code, process, _, _ = self.invoke(
            ["plan"], fallback=str(ROOT / "never-created-private.env")
        )
        self.assertIn("outside the repository", exit_code)
        process.assert_not_called()

    def test_invalid_volume_prefix_is_rejected_before_docker(self):
        """A data prefix must obey the same name rules as the project."""
        content = self.env_file.read_text().replace(
            "DATA_VOLUME_PREFIX=", "DATA_VOLUME_PREFIX=invalid/name"
        )
        self.env_file.write_text(content)
        exit_code, process, _, _ = self.invoke(
            ["plan"], fallback=str(self.env_file)
        )
        self.assertIn("Invalid DATA_VOLUME_PREFIX", exit_code)
        process.assert_not_called()

    def test_upgrade_preserves_existing_keys_and_adds_missing_keys(self):
        """Upgrade an older site without rotating encryption or API keys."""
        original = self.env_file.read_text()
        kept = [
            line
            for line in original.splitlines()
            if not line.startswith(("MCP_", "SUPERSET_", "JUPYTER_"))
        ]
        old = "\n".join(kept) + "\n"
        self.env_file.write_text(old)
        arguments = [
            str(CONFIGURE),
            "--output",
            str(self.env_file),
            "--upgrade",
        ]
        with (
            mock.patch.dict(sys.modules, {"settings": SETTINGS}),
            mock.patch.object(sys, "argv", arguments),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(str(CONFIGURE), run_name="__main__")
        upgraded = SETTINGS.load(self.env_file)
        before = dict(
            line.split("=", 1)
            for line in kept
            if line and not line.startswith("#")
        )
        for key, value in before.items():
            self.assertEqual(upgraded[key], value)
        self.assertEqual(len(upgraded["MCP_API_TOKEN"]), 64)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        backup = self.env_file.with_name(
            self.env_file.name + ".before-upgrade"
        )
        self.assertEqual(backup.read_text(), old)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_new_profiles_validate_identity_and_separate_vector_keys(self):
        """Reject settings which would fail or erase the privilege boundary."""
        original = self.env_file.read_text()
        cases = [
            ("vector", "QDRANT_READ_ONLY_API_KEY", "QDRANT_API_KEY"),
            ("notebooks", "WORKER_UID", "0"),
            ("notebooks", "JUPYTER_TOKEN", "a" * 20),
            ("bi", "SUPERSET_SECRET_KEY", "a" * 20),
        ]
        for profile, key, replacement in cases:
            with self.subTest(profile=profile, key=key):
                lines = dict(
                    line.split("=", 1)
                    for line in original.splitlines()
                    if line and not line.startswith("#")
                )
                lines["COMPOSE_PROFILES"] = profile
                lines[key] = lines.get(replacement, replacement)
                self.env_file.write_text(
                    "\n".join(
                        key + "=" + value for key, value in lines.items()
                    )
                    + "\n"
                )
                with self.assertRaises(ValueError):
                    SETTINGS.load(self.env_file)

    def test_ssrf_rules_reject_typos_before_deployment(self):
        """Do not silently skip invalid policy entries as upstream may do."""
        valid = {"N8N_SSRF_ALLOWED_HOSTNAMES": "worker,*.example.invalid"}
        SETTINGS.validate_ssrf(valid)
        self.assertEqual(valid["N8N_SSRF_PROTECTION_ENABLED"], "true")
        invalid = [
            {"N8N_SSRF_ALLOWED_IP_RANGES": "not-a-network"},
            {"N8N_SSRF_BLOCKED_IP_RANGES": "defaut"},
            {"N8N_SSRF_ALLOWED_HOSTNAMES": "http://worker:8000"},
            {"N8N_SSRF_ALLOWED_HOSTNAMES": "*"},
            {"N8N_SSRF_PROTECTION_ENABLED": "yes"},
            {"N8N_SSRF_DNS_CACHE_MAX_SIZE": "-1"},
        ]
        for values in invalid:
            with self.subTest(keys=list(values)):
                with self.assertRaises(ValueError):
                    SETTINGS.validate_ssrf(values)

    def test_inactive_profile_ports_do_not_break_existing_sites(self):
        """A new optional default must not reserve a port until enabled."""
        content = self.env_file.read_text().replace(
            "TRINO_PORT=8080", "TRINO_PORT=8082"
        )
        self.env_file.write_text(content)
        self.assertEqual(SETTINGS.load(self.env_file)["TRINO_PORT"], "8082")
        content = content.replace(
            "COMPOSE_PROFILES=", "COMPOSE_PROFILES=compute"
        )
        self.env_file.write_text(content)
        with self.assertRaisesRegex(ValueError, "Active host service ports"):
            SETTINGS.load(self.env_file)


@unittest.skipUnless(
    os.name == "posix"
    and os.geteuid() != 0
    and all(
        hasattr(os, key) for key in ("O_PATH", "O_NOFOLLOW", "O_DIRECTORY")
    ),
    "Metadata tests require a nonroot Linux UID and disposable directories.",
)
class MonitoringSpoolTests(ControlFixture, unittest.TestCase):
    """Inspect only owned temporary metadata; never run Docker or a probe."""

    def setUp(self):
        super().setUp()
        with mock.patch.object(
            sys, "path", [str(ROOT / "scripts"), *sys.path]
        ):
            import notebook_workspace
        original_safe = notebook_workspace.parent_safe

        def synthetic_root(metadata, path, uid):
            # Sandbox root/tmp may be mapped to nobody. Model normal root
            # ownership only for these fixed ancestors in disposable tests;
            # production checks and all selected directory metadata stay real.
            if str(path) in {"/", "/tmp"} and metadata.st_uid == 65534:
                metadata = SimpleNamespace(st_uid=0, st_mode=metadata.st_mode)
            return original_safe(metadata, path, uid)

        patched = mock.patch.object(
            notebook_workspace, "parent_safe", synthetic_root
        )
        patched.start()
        self.addCleanup(patched.stop)

    def configure_monitoring(self, spool, *, profiles="monitoring"):
        """Choose temporary landing/spool paths in synthetic settings."""
        self.landing = self.env_file.parent.parent / "landing"
        self.landing.mkdir(mode=0o700, exist_ok=True)
        values = dict(
            line.split("=", 1)
            for line in self.env_file.read_text().splitlines()
            if line and not line.startswith("#")
        )
        values.update(
            COMPOSE_PROFILES=profiles,
            LANDING_DIR=str(self.landing),
            MONITORING_LOG_DIR=str(spool),
        )
        self.env_file.write_text(
            "\n".join(key + "=" + value for key, value in values.items())
            + "\n"
        )

    def invoke_action(self, action):
        """Keep every subprocess and Grafana seed synthetic."""
        with mock.patch.dict(
            sys.modules,
            {
                "grafana_setup": mock.Mock(
                    setup=mock.Mock(return_value={"status": "exists"})
                )
            },
        ):
            return self.invoke(["--env-file", str(self.env_file), action])

    def test_missing_spool_blocks_up_and_predeploy_before_compose(self):
        spool = self.env_file.parent.parent / "missing-spool"
        self.configure_monitoring(spool)
        for action in ("up", "predeploy"):
            with self.subTest(action=action):
                code, process, output, _ = self.invoke_action(action)
                self.assertIn("MONITORING_LOG_DIR is not ready", code)
                self.assertFalse(json.loads(output)["ready"])
                self.assertNotIn(str(spool), output)
                process.assert_not_called()
                self.assertFalse(spool.exists())

    def test_file_symlinks_and_unsafe_modes_never_reach_compose(self):
        base = self.env_file.parent.parent
        cases = {}
        regular = base / "regular"
        regular.write_bytes(b"synthetic preserved bytes")
        cases["regular"] = regular
        target = base / "target"
        target.mkdir(mode=0o700)
        link = base / "link"
        link.symlink_to(target, target_is_directory=True)
        cases["leaf-link"] = link
        dangling = base / "dangling"
        dangling.symlink_to(base / "absent", target_is_directory=True)
        cases["dangling-link"] = dangling
        cases["ancestor-link"] = link / "spool"
        unsafe = base / "unsafe"
        unsafe.mkdir(mode=0o755)
        cases["unsafe-mode"] = unsafe
        for name, spool in cases.items():
            self.configure_monitoring(spool)
            for action in ("up", "predeploy"):
                with self.subTest(case=name, action=action):
                    code, process, _, _ = self.invoke_action(action)
                    self.assertIsNotNone(code)
                    process.assert_not_called()
        self.assertEqual(regular.read_bytes(), b"synthetic preserved bytes")
        self.assertTrue(link.is_symlink())
        self.assertTrue(dangling.is_symlink())
        self.assertEqual(unsafe.stat().st_mode & 0o7777, 0o755)

    def test_foreign_directory_uid_or_gid_is_rejected_without_chown(self):
        spool = self.env_file.parent.parent / "spool"
        spool.mkdir(mode=0o700)
        self.configure_monitoring(spool)
        selected = spool.stat()
        original_fstat = os.fstat
        for field in (4, 5):

            def inspect(descriptor, field=field):
                metadata = original_fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == (
                    selected.st_dev,
                    selected.st_ino,
                ):
                    changed = list(metadata)
                    changed[field] += 1
                    return os.stat_result(changed)
                return metadata

            for action in ("up", "predeploy"):
                with (
                    self.subTest(field=field, action=action),
                    mock.patch("os.fstat", side_effect=inspect),
                    mock.patch("os.chown", side_effect=AssertionError),
                ):
                    code, process, output, _ = self.invoke_action(action)
                    self.assertIn("MONITORING_LOG_DIR is not ready", code)
                    self.assertEqual(
                        json.loads(output)["reason"],
                        "MONITORING_DIRECTORY_OWNER_MISMATCH",
                    )
                    process.assert_not_called()
        self.assertEqual(
            (spool.stat().st_uid, spool.stat().st_gid),
            (selected.st_uid, selected.st_gid),
        )

    def test_unsafe_parent_blocks_without_repair(self):
        parent = self.env_file.parent.parent / "shared-parent"
        parent.mkdir(mode=0o775)
        parent.chmod(0o775)
        spool = parent / "spool"
        spool.mkdir(mode=0o700)
        self.configure_monitoring(spool)
        code, process, output, _ = self.invoke_action("up")
        self.assertIn("MONITORING_LOG_DIR is not ready", code)
        self.assertEqual(
            json.loads(output)["reason"], "MONITORING_PARENT_UNSAFE"
        )
        process.assert_not_called()
        self.assertEqual(parent.stat().st_mode & 0o7777, 0o775)

    def test_existing_private_spool_is_read_only_and_precedes_build(self):
        spool = self.env_file.parent.parent / "spool"
        spool.mkdir(mode=0o700)
        data = spool / "existing-private.jsonl"
        data.write_bytes(b"synthetic unchanged log bytes\n")
        data.chmod(0o600)
        self.configure_monitoring(spool)
        original_open = os.open

        def open_read_only(path, flags, *arguments, **options):
            self.assertFalse(
                flags & (os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_RDWR)
            )
            return original_open(path, flags, *arguments, **options)

        for mode in (0o700, 0o750):
            spool.chmod(mode)
            before = spool.stat()
            for action in ("up", "predeploy"):
                with (
                    self.subTest(mode=mode, action=action),
                    mock.patch("os.open", side_effect=open_read_only),
                    mock.patch("os.mkdir", side_effect=AssertionError),
                    mock.patch("os.chmod", side_effect=AssertionError),
                    mock.patch("os.chown", side_effect=AssertionError),
                    mock.patch("os.unlink", side_effect=AssertionError),
                    mock.patch("os.write", side_effect=AssertionError),
                ):
                    code, process, output, _ = self.invoke_action(action)
                self.assertIsNone(code)
                report = json.loads(output.splitlines()[0])
                self.assertEqual(report["setting"], "MONITORING_LOG_DIR")
                self.assertTrue(report["ready"])
                self.assertFalse(report["mutated"])
                self.assertEqual(report["probe"], "not_run")
                commands = [call.args[0] for call in process.call_args_list]
                if action == "up":
                    self.assertIn("up", commands[0])
                    self.assertIn("--build", commands[0])
                    self.assertEqual(commands[1][-1], "bootstrap")
                else:
                    self.assertEqual(commands[0][-2:], ["config", "--quiet"])
            after = spool.stat()
            self.assertEqual(
                (
                    before.st_mode,
                    before.st_uid,
                    before.st_gid,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ),
                (
                    after.st_mode,
                    after.st_uid,
                    after.st_gid,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ),
            )
        self.assertEqual(data.read_bytes(), b"synthetic unchanged log bytes\n")

    def test_plan_is_configuration_only_and_profile_off_needs_no_spool(self):
        spool = self.env_file.parent.parent / "missing-spool"
        self.configure_monitoring(spool)
        code, process, output, _ = self.invoke_action("plan")
        self.assertIsNone(code)
        self.assertNotIn("MONITORING_LOG_DIR", output)
        self.assertEqual(process.call_args.args[0][-2:], ["config", "--quiet"])
        self.configure_monitoring(spool, profiles="")
        for action in ("up", "predeploy"):
            with self.subTest(action=action):
                code, process, output, _ = self.invoke_action(action)
                self.assertIsNone(code)
                self.assertNotIn("MONITORING_LOG_DIR", output)
                self.assertTrue(process.called)
                self.assertFalse(spool.exists())


if __name__ == "__main__":
    unittest.main()
