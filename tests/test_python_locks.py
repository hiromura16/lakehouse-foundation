"""Check Renovate-managed Python input, generation recipes, and hash locks."""

import json
import re
import unittest
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"([\w.-]+)(?:\[[^]]+\])?==([^\s;]+)(?:\s*;.+)?")
HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


class PythonLockTests(unittest.TestCase):
    """Reject source-only dependency updates and missing lock hashes."""

    def test_renovate_python_locks_match_their_inputs(self):
        config = json.loads((ROOT / "renovate.json").read_text())
        locks = config["pip-compile"]["managerFilePatterns"]
        self.assertTrue(locks)

        for lock_name in locks:
            with self.subTest(lock=lock_name):
                lock_path = Path(lock_name)
                source_path = lock_path.with_suffix(".txt")
                lock_text = (ROOT / lock_path).read_text()
                command = lock_text.splitlines()[1]
                recipe = command.removeprefix("#    ").split()
                self.assertEqual(recipe[:3], ["uv", "pip", "compile"])
                self.assertIn("--generate-hashes", recipe)
                self.assertIn(f"--output-file={lock_path}", recipe)
                self.assertIn(str(source_path), recipe)

                locked = self._locked_versions(lock_text)
                for line in (ROOT / source_path).read_text().splitlines():
                    requirement = line.partition("#")[0].strip()
                    if requirement:
                        name, version = self._parse_pin(requirement)
                        self.assertEqual(locked[name], {version}, name)

    def _locked_versions(self, text):
        """Read named pins and their hash continuations from a uv output."""
        versions = defaultdict(set)
        for line in text.replace("\\\n", "").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                requirement, _, hashes = line.partition(" --hash=")
                self.assertTrue(hashes, f"Missing hashes for {requirement}")
                for token in ("--hash=" + hashes).split():
                    self.assertRegex(token, HASH)
                name, version = self._parse_pin(requirement.strip())
                versions[name].add(version)
        self.assertTrue(versions)
        return versions

    def _parse_pin(self, requirement):
        """Parse a named exact pin, allowing extras and environment markers."""
        match = PIN.fullmatch(requirement)
        self.assertIsNotNone(match, f"Expected an exact pin: {requirement}")
        name, version = match.groups()
        return re.sub(r"[-_.]+", "-", name).lower(), version
