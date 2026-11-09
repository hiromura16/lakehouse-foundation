"""Check private proxy configuration without applying host-level settings."""

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/proxy_settings.py"
WRAPPER = ROOT / "scripts/configure-proxy.sh"
SPEC = importlib.util.spec_from_file_location("proxy_settings_subject", SCRIPT)
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)
TEMPLATE = (ROOT / "infra/proxy/proxy.env.example").read_text()
DAEMON = Path("/etc/docker/daemon.json")
TARGET_ENV = Path("/etc/lakehouse-foundation/proxy.env")


def invoke(mode, source):
    """Run the Python entry point and retain only its non-secret output."""
    output = io.StringIO()
    arguments = [str(SCRIPT), mode, str(source), str(ROOT)]
    with mock.patch.object(sys, "argv", arguments):
        with contextlib.redirect_stdout(output):
            try:
                PROXY.main()
            except SystemExit as error:
                return error.code, output.getvalue()
    return 0, output.getvalue()


class ProxySettingsTests(unittest.TestCase):
    """Cover the wrapper default and Python validation/write boundaries."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="proxy-settings-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "private/proxy.env"
        self.source.parent.mkdir(mode=0o700)
        self.source.write_text(TEMPLATE)
        self.source.chmod(0o600)
        # Simulated apply must never reach a host configuration write.
        self.writer_patch = mock.patch.object(PROXY, "atomic_write")
        self.writer = self.writer_patch.start()
        self.addCleanup(self.writer_patch.stop)

    def assert_refused(self, text=None):
        """Expect validation failure with no destination writes."""
        if text is not None:
            self.source.write_text(text)
        code, _ = invoke("plan", self.source)
        self.assertIsInstance(code, str)
        self.assertTrue(code.startswith("ERROR:"))
        self.writer.assert_not_called()
        return code

    def test_wrapper_defaults_to_read_only_plan(self):
        original = self.source.read_bytes()
        result = subprocess.run(
            [str(WRAPPER), "--env-file", str(self.source)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Plan: install", result.stdout)
        self.assertIn("until an explicit restart", result.stdout)
        self.assertEqual(self.source.read_bytes(), original)

    def test_plan_validates_without_installing(self):
        code, output = invoke("plan", self.source)
        self.assertEqual(code, 0)
        self.assertIn("root:root 0600", output)
        self.writer.assert_not_called()

    def test_init_creates_a_private_template_and_refuses_overwrite(self):
        destination = self.directory / "new-private/proxy.env"
        code, _ = invoke("init", destination)
        self.assertEqual(code, 0)
        self.assertEqual(destination.read_text(), TEMPLATE)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(destination.parent.stat().st_mode & 0o777, 0o700)
        code, _ = invoke("init", destination)
        self.assertIn("already exists", code)
        self.writer.assert_not_called()

    def test_encoded_credentials_do_not_enter_output(self):
        secret = "sentinel%23credential"
        proxy = f"http://synthetic:{secret}@proxy.example:3128"
        contents = TEMPLATE.replace("HTTP_PROXY=", "HTTP_PROXY=" + proxy, 1)
        contents = contents.replace("http_proxy=", "http_proxy=" + proxy, 1)
        self.source.write_text(contents)
        code, output = invoke("plan", self.source)
        self.assertEqual(code, 0)
        self.assertNotIn(secret, output)
        self.assertNotIn(proxy, output)
        self.writer.assert_not_called()

    def test_readable_by_others_is_rejected(self):
        self.source.chmod(0o644)
        self.assertIn("private", self.assert_refused())

    def test_unknown_duplicate_missing_and_mismatched_keys_are_rejected(self):
        variations = [
            TEMPLATE + "UNEXPECTED_KEY=synthetic\n",
            TEMPLATE + "HTTP_PROXY=\n",
            TEMPLATE.replace("HTTPS_PROXY=\n", "", 1),
            TEMPLATE.replace(
                "http_proxy=", "http_proxy=http://different.example:3128", 1
            ),
        ]
        for contents in variations:
            with self.subTest(contents_length=len(contents)):
                self.assert_refused(contents)

    def test_shell_expansion_and_invalid_urls_are_rejected(self):
        for value in ["$(forbidden)", "http://proxy.example", "file:///tmp/x"]:
            with self.subTest(value=value):
                contents = TEMPLATE.replace(
                    "HTTP_PROXY=", "HTTP_PROXY=" + value, 1
                )
                contents = contents.replace(
                    "http_proxy=", "http_proxy=" + value, 1
                )
                self.assert_refused(contents)

    def test_symlinks_and_repository_destinations_are_refused(self):
        linked = self.directory / "linked.env"
        linked.symlink_to(self.source)
        code, _ = invoke("plan", linked)
        self.assertIn("Symlinks", code)
        # The path need not exist: rejection precedes every creation attempt.
        code, _ = invoke("init", ROOT / "proxy-should-never-exist.env")
        self.assertIn("outside the repository", code)
        self.assertFalse((ROOT / "proxy-should-never-exist.env").exists())
        self.writer.assert_not_called()

    def test_apply_requires_root(self):
        with (
            mock.patch.object(PROXY.os, "geteuid", return_value=65534),
            mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
        ):
            code, _ = invoke("apply", self.source)
        self.assertIn("requires sudo/root", code)
        self.writer.assert_not_called()

    def test_daemon_proxy_conflict_is_refused_before_installation(self):
        original_exists = Path.exists
        original_read = Path.read_text

        def exists(path):
            return path == DAEMON or original_exists(path)

        def read(path, *args, **kwargs):
            if path == DAEMON:
                return '{"proxies": {"http-proxy": "http://example:3128"}}'
            return original_read(path, *args, **kwargs)

        with (
            mock.patch.object(PROXY.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
            mock.patch.object(Path, "exists", exists),
            mock.patch.object(Path, "read_text", read),
        ):
            code, _ = invoke("apply", self.source)
        self.assertIn("already defines proxies", code)
        self.writer.assert_not_called()

    def test_unmanaged_destination_is_never_overwritten(self):
        original_exists = Path.exists
        original_read = Path.read_text

        def exists(path):
            if path == DAEMON:
                return False
            return path == TARGET_ENV or original_exists(path)

        def read(path, *args, **kwargs):
            if path == TARGET_ENV:
                return "unmanaged existing configuration\n"
            return original_read(path, *args, **kwargs)

        with (
            mock.patch.object(PROXY.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
            mock.patch.object(Path, "exists", exists),
            mock.patch.object(Path, "read_text", read),
        ):
            code, _ = invoke("apply", self.source)
        self.assertIn("unmanaged destination", code)
        self.writer.assert_not_called()

    def test_simulated_apply_only_prepares_two_managed_files(self):
        original_exists = Path.exists

        def exists(path):
            if path == DAEMON or str(path).startswith("/etc/"):
                return False
            return original_exists(path)

        with (
            mock.patch.object(PROXY.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}),
            mock.patch.object(Path, "exists", exists),
        ):
            code, output = invoke("apply", self.source)
        self.assertEqual(code, 0)
        self.assertEqual(self.writer.call_count, 2)
        first, second = self.writer.call_args_list
        self.assertEqual(first.args[0], TARGET_ENV)
        self.assertEqual(first.args[2], 0o600)
        self.assertEqual(second.args[2], 0o644)
        self.assertTrue(
            first.args[1].startswith(
                "# managed-by=lakehouse-foundation-proxy\n"
            )
        )
        self.assertIn("Docker has NOT been restarted", output)


if __name__ == "__main__":
    unittest.main()
