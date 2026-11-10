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

    def test_svg_geometry_decimal_sequences_are_not_addresses(self):
        """Accept compressed SVG numbers without weakening other formats."""
        host = ".".join(["203", "0", "113", "71"])
        text = (
            '<svg xmlns="http://www.w3.org/2000/svg"\n'
            ' viewBox="' + host + ' 0"><title>合成の図形</title>\n'
            '<path d="M ' + host + ' 0"/>\n'
            '<polygon points="' + host + ' 0"/>\n'
            '<g transform="matrix(' + host + ' 0 0 0)"/>\n'
            "</svg>"
        )
        self.assertEqual(
            MODULE.scan_text("fixture.svg", text, set(), set()), []
        )
        findings = MODULE.scan_text("fixture.txt", text, set(), set())
        self.assertIn(
            "concrete-ipv4-address", {item.rule for item in findings}
        )

    def test_svg_links_text_and_non_geometry_keep_address_detection(self):
        """Scrub actual geometry attributes, never links, text or comments."""
        host = ".".join(["203", "0", "113", "71"])
        fragments = [
            '<a href="http://' + host + '/"/>',
            "<title>" + host + "</title>",
            "<text>" + host + "</text>",
            '<g data-note="' + host + '"/>',
            '<!-- <path d="M ' + host + ' 0"/> -->',
            '<![CDATA[<path d="M ' + host + ' 0"/>]]>',
            '<other:path xmlns:other="urn:example" d="M ' + host + ' 0"/>',
        ]
        for fragment in fragments:
            with self.subTest(fragment=fragment.split()[0]):
                text = (
                    '<svg xmlns="http://www.w3.org/2000/svg">\n'
                    '<path d="M ' + host + ' 0"/>\n' + fragment + "\n</svg>"
                )
                findings = MODULE.scan_text("fixture.svg", text, set(), set())
                ips = [
                    item
                    for item in findings
                    if item.rule == "concrete-ipv4-address"
                ]
                self.assertEqual([item.line for item in ips], [3])
        unmasked = [
            '<svg><path d="M ' + host + ' 0">',
            '<!DOCTYPE svg><svg><path d="M ' + host + ' 0"/></svg>',
        ]
        for text in unmasked:
            self.assertIn(
                "concrete-ipv4-address",
                {
                    item.rule
                    for item in MODULE.scan_text(
                        "fixture.svg", text, set(), set()
                    )
                },
            )

    def test_svg_geometry_keeps_full_secret_and_deny_checks(self):
        """Inspect original geometry for credentials, deny terms and tokens."""
        secret = "123456789" * 3
        denied = ".".join(["203", "0", "113", "71"])
        token = "ghp_" + "A" * 36
        text = (
            '<svg><path d="M ' + denied + " 0 " + secret + ' 0"/>'
            '<path d="M0 0 ' + token + '"/></svg>'
        )
        findings = MODULE.scan_text("fixture.svg", text, {denied}, {secret})
        rules = {item.rule for item in findings}
        self.assertIn("private-deny-term", rules)
        self.assertIn("private-credential-value", rules)
        self.assertIn("github-token", rules)
        self.assertNotIn("concrete-ipv4-address", rules)

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

    def test_only_exact_empty_ca_placeholder_is_allowed(self):
        """Keep the PEM path ban except for the single zero-byte source."""
        allowed = "infra/proxy/empty-ca.pem"
        self.assertEqual(
            MODULE.scan_blob(allowed, b"", set(), set(), set()), []
        )
        names = [
            "other.pem",
            "infra/proxy/other.pem",
            "infra/proxy/empty-ca.PEM",
            "infra/proxy/nested/empty-ca.pem",
            "infra/proxy/./empty-ca.pem",
        ]
        for name in names:
            with self.subTest(name=name):
                findings = MODULE.scan_blob(name, b"", set(), set(), set())
                self.assertIn(
                    "private-or-runtime-file", {item.rule for item in findings}
                )

    def test_placeholder_must_be_zero_bytes_and_keeps_content_checks(self):
        """Reject whitespace, BOM and secrets rather than skipping the file."""
        name = "infra/proxy/empty-ca.pem"
        token = "ghp_" + "C" * 36
        header = "-----BEGIN " + "RSA PRIVATE " + "KEY-----"
        contents = [b"\n", b"\xef\xbb\xbf", b"public certificate"]
        contents.append((header + "\n" + token).encode())
        for content in contents:
            with self.subTest(size=len(content)):
                findings = MODULE.scan_blob(name, content, set(), set(), set())
                self.assertIn(
                    "private-or-runtime-file", {item.rule for item in findings}
                )
        findings = MODULE.scan_blob(name, contents[-1], set(), set(), set())
        self.assertIn("private-key", {item.rule for item in findings})
        self.assertIn("github-token", {item.rule for item in findings})

    def test_placeholder_staged_and_working_contents_are_both_checked(self):
        """A clean copy cannot hide a changed staged blob, or vice versa."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            path = root / "infra/proxy/empty-ca.pem"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"")
            self.commit(root, "Add synthetic empty placeholder")
            self.assertEqual(self.inspect(root).returncode, 0)
            path.write_text("not empty")
            result = self.inspect(root)
            self.assertEqual(result.returncode, 1)
            subprocess.run(
                ["git", "-C", str(root), "add", "--all"], check=True
            )
            path.write_bytes(b"")
            result = self.inspect(root)
            self.assertEqual(result.returncode, 1)
            self.assertIn("private-or-runtime-file", result.stdout)

    def test_placeholder_and_other_pem_history_keep_rejections(self):
        """Allow empty history but reject deleted nonempty and other PEMs."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            path = root / "infra/proxy/empty-ca.pem"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"")
            self.commit(root, "Add synthetic empty placeholder")
            self.assertEqual(self.inspect(root, "--history").returncode, 0)
            token = "ghp_" + "D" * 36
            path.write_text(token)
            other = root / "other.pem"
            other.write_bytes(b"")
            self.commit(root, "Synthetic forbidden historical PEM files")
            path.write_bytes(b"")
            other.unlink()
            self.commit(root, "Restore empty placeholder and remove other")
            self.assertEqual(self.inspect(root).returncode, 0)
            result = self.inspect(root, "--history")
            self.assertEqual(result.returncode, 1)
            self.assertIn("history:", result.stdout)
            self.assertIn(":infra/proxy/empty-ca.pem", result.stdout)
            self.assertIn(":other.pem", result.stdout)
            self.assertIn("github-token", result.stdout)
            self.assertNotIn(token, result.stdout + result.stderr)

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

    def test_private_scripts_are_not_parsed_as_env_assignments(self):
        """Keep real env secrets while rejecting shell comparison noise."""
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "source"
            root.mkdir()
            private = parent / "private"
            private.mkdir()
            command = 'test "$$API_KEY" != "$$READ_ONLY_API_KEY";'
            (private / "script.sh").write_text(command + "\n")
            (root / "example.sh").write_text(command + "\n")
            result = self.inspect(root, "--secret-dir", str(private))
            self.assertEqual(result.returncode, 0, result.stdout)
            secret = "SyntheticCredential" + "7" * 24
            (private / "site.env").write_text("API_KEY=" + secret + "\n")
            (root / "leaked.txt").write_text(secret)
            result = self.inspect(root, "--secret-dir", str(private))
            self.assertEqual(result.returncode, 1)
            self.assertIn("private-credential-value", result.stdout)
            self.assertNotIn(secret, result.stdout + result.stderr)

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
