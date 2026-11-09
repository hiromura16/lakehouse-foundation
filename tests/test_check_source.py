"""Test publication checks with synthetic values and temporary Git state."""

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check-source.py"
SPEC = importlib.util.spec_from_file_location("source_checker", CHECKER)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class PublicationChecks(unittest.TestCase):
    """Exercise leak detection without including real private values."""

    def inspect(self, root, *options):
        """Run the same public CLI as CI and capture value-free diagnostics."""
        return subprocess.run(
            [sys.executable, str(CHECKER), "--root", str(root), *options],
            capture_output=True,
            text=True,
            check=False,
        )

    def commit(self, root, message):
        """Create only local synthetic commits with explicit test metadata."""
        subprocess.run(
            ["git", "-C", str(root), "add", "--all"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=SourceTest",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                message,
            ],
            check=True,
            capture_output=True,
        )

    def test_network_constants_and_hosts_are_distinct(self):
        """Allow documented ranges while rejecting specific nonlocal hosts."""
        self.assertTrue(MODULE.permitted_ip("127.0.0.1"))
        self.assertTrue(MODULE.permitted_ip("0.0.0.0"))
        self.assertTrue(MODULE.permitted_ip("172.30.0.0/16"))
        shared = ".".join(["100", "64", "0", "0"])
        self.assertTrue(MODULE.permitted_ip(shared + "/10"))
        self.assertFalse(MODULE.permitted_ip(shared + "/32"))
        host = ".".join(["203", "0", "113", "71"])
        self.assertFalse(MODULE.permitted_ip(host))
        self.assertFalse(MODULE.permitted_ip(host + "/32"))

    def test_tokens_home_paths_and_private_artifacts(self):
        """Find policy violations without echoing synthetic contents."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = "ghp_" + "A" * 36
            home = "/" + "home" + "/" + "example_account" + "/private"
            header = "-----BEGIN " + "RSA PRIVATE " + "KEY-----"
            (root / "bad.txt").write_text(
                token + "\n" + home + "\n" + header + "\n"
            )
            (root / ".env").write_text("PASSWORD=synthetic-test-value\n")
            (root / ("leak-" + token + ".txt")).write_text("Public text.")
            result = self.inspect(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("github-token", result.stdout)
            self.assertIn("private-key", result.stdout)
            self.assertIn("personal-home-path", result.stdout)
            self.assertIn("private-env-file", result.stdout)
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertNotIn(home, result.stdout + result.stderr)

    def test_external_deny_terms_and_credentials_stay_out_of_output(self):
        """Load private inputs externally and report only their rule names."""
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            private = parent / "credentials"
            private.mkdir()
            term = "synthetic" + "-" + "machine"
            password = "Unpublished" + "9" * 24
            deny = parent / "deny.txt"
            deny.write_text(term + "\n")
            (private / "service.json").write_text(
                '{"password": "' + password + '"}'
            )
            (root / "notes.txt").write_text(term + "\n" + password)
            result = self.inspect(
                root,
                "--deny-file",
                str(deny),
                "--secret-dir",
                str(private),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("private-deny-term", result.stdout)
            self.assertIn("private-credential-value", result.stdout)
            self.assertNotIn(term, result.stdout + result.stderr)
            self.assertNotIn(password, result.stdout + result.stderr)
            result = self.inspect(root, "--deny-file", str(root / "notes.txt"))
            self.assertEqual(result.returncode, 2)

    def test_deleted_history_and_staged_content_are_checked(self):
        """Detect deleted leaks and staged leaks hidden by clean files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            token = "ghp_" + "B" * 36
            path = root / "past.txt"
            path.write_text(token)
            self.commit(root, "Synthetic original")
            path.unlink()
            (root / "README.md").write_text("Public source.\n")
            self.commit(root, "Remove synthetic old file")
            self.assertEqual(self.inspect(root).returncode, 0)
            result = self.inspect(root, "--history")
            self.assertEqual(result.returncode, 1)
            self.assertIn("history:", result.stdout)
            self.assertNotIn(token, result.stdout + result.stderr)
            path.write_text(token)
            subprocess.run(
                ["git", "-C", str(root), "add", "past.txt"], check=True
            )
            path.write_text("Clean worktree contents.\n")
            self.assertEqual(self.inspect(root).returncode, 1)

    def test_binary_files_need_an_explicit_png_allowlist(self):
        """Reject unreviewed binary sources even with a picture extension."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixture.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            self.assertEqual(self.inspect(root).returncode, 1)
            result = self.inspect(root, "--allow-png", "fixture.png")
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
