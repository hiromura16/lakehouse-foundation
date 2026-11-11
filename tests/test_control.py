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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "scripts/control.py"
CONFIGURE = ROOT / "scripts/configure.py"
SPEC = importlib.util.spec_from_file_location(
    "control_settings_subject", ROOT / "scripts/settings.py"
)
SETTINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETTINGS)


class ControlEnvironmentTests(unittest.TestCase):
    """Select the intended file and retain the real private-file validator."""

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


if __name__ == "__main__":
    unittest.main()
