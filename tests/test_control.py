"""Check private environment selection without Docker or network access."""

import contextlib
import importlib.util
import io
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


if __name__ == "__main__":
    unittest.main()
