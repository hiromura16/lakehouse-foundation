"""Validate DBA input generation without SQL clients or private site files."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
    SPEC = importlib.util.spec_from_file_location(
        "prepare_sqlserver_subject", ROOT / "scripts/prepare-sqlserver.py"
    )
    PREPARE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(PREPARE)


def values():
    """Return clearly synthetic identifiers and generated-format ownership."""
    return {
        "DEMO_SQLSERVER_DATABASE": "fixture_database",
        "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
        "DEMO_SQLSERVER_USER": "fixture_writer",
        "SOURCE_SQLSERVER_USER": "fixture_reader",
        "SOURCE_SQLSERVER_CONTROL_USER": "fixture_control",
    }


class PrepareSqlTests(unittest.TestCase):
    """Reject substitution attacks, preserve secrets and refuse overwrite."""

    def test_only_hash_and_approved_identifiers_enter_dba_input(self):
        selected = values()
        output = PREPARE.prepare(selected, "c" * 40)
        token = selected["DEMO_SQLSERVER_OWNERSHIP_TOKEN"]
        self.assertNotIn(token, output)
        self.assertIn(sha256(token.encode()).hexdigest(), output)
        self.assertIn(':setvar TargetDatabase "fixture_database"', output)
        self.assertNotIn("PASSWORD", output)
        self.assertIn(':setvar ConfirmDisableCdc "0"', output)

    def test_sqlcmd_injection_and_reused_credentials_are_rejected(self):
        for changes in (
            {"DEMO_SQLSERVER_DATABASE": "fixture'; DROP DATABASE x;--"},
            {"SOURCE_SQLSERVER_USER": 'fixture"\n:r invalid.sql'},
            {"SOURCE_SQLSERVER_CONTROL_USER": "fixture_reader"},
            {"DEMO_SQLSERVER_OWNERSHIP_TOKEN": "invalid"},
        ):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(ValueError):
                    PREPARE.prepare({**values(), **changes}, "c" * 40)

    def test_cli_writes_private_input_without_connecting_or_disclosing(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "private/site.env"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/configure.py"),
                    "--output",
                    str(private),
                ],
                capture_output=True,
                check=True,
            )
            content = private.read_text()
            for key, value in values().items():
                lines = content.splitlines()
                content = (
                    "\n".join(
                        key + "=" + value
                        if line.startswith(key + "=")
                        else line
                        for line in lines
                    )
                    + "\n"
                )
            private.write_text(content)
            output = private.with_name("dba.sql")
            command = [
                sys.executable,
                str(ROOT / "scripts/prepare-sqlserver.py"),
                "--env-file",
                str(private),
                "--output",
                str(output),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("fixture_writer", result.stdout + result.stderr)
            original = output.read_bytes()
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
