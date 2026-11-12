"""Protect private notes in Git and declare the Docker build-context rule.

The Git test runs in a disposable synthetic repository. Docker assertions are
static source contracts only; they do not claim an actual image was built.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PrivateDocumentBoundaryTests(unittest.TestCase):
    """Keep local design notes out of publication and COPY docs inputs."""

    def test_private_notes_are_ignored_by_a_fresh_git_checkout(self):
        """Shared rules work without the author's local info/exclude file."""
        with tempfile.TemporaryDirectory(prefix="private-doc-test-") as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_bytes(
                (ROOT / ".gitignore").read_bytes()
            )
            private = root / "docs/private"
            private.mkdir(parents=True)
            (private / "proposal.md").write_text("Synthetic private note.")
            (root / "docs/guide.md").write_text("Public guide.")
            subprocess.run(
                ["git", "init", "-q", str(root)],
                check=True,
                capture_output=True,
            )
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("docs/guide.md", result.stdout)
            self.assertNotIn("docs/private/", result.stdout)
            subprocess.run(
                ["git", "check-ignore", "-q", "docs/private/proposal.md"],
                cwd=root,
                check=True,
            )

    def test_private_context_exclusion_is_final_after_copy_sources(self):
        """A final exclusion cannot be undone by a later Docker negation."""
        entries = [
            line.strip()
            for line in (ROOT / ".dockerignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(entries[-1], "/docs/private/")
        dockerfile = (ROOT / "infra/demo/Dockerfile").read_text()
        self.assertIn("COPY docs ./docs", dockerfile)
        self.assertIn("COPY infra/demo ./infra/demo", dockerfile)


if __name__ == "__main__":
    unittest.main()
