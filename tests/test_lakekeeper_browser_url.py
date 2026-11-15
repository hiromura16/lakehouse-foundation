"""Render browser-only Lakekeeper URLs without a Docker daemon."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("docker"), "Compose CLI is unavailable")
class LakekeeperBrowserUrlTests(unittest.TestCase):
    """Keep browser routing separate from internal Iceberg client routing."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalog-ui-config-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.site = self.directory / "private" / "site.env"
        self.environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "HOME": str(self.directory),
            "DOCKER_CONFIG": str(self.directory / "docker-config"),
            # A configuration renderer must never need the real daemon.
            "DOCKER_HOST": "unix://" + str(self.directory / "absent.sock"),
        }
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(self.site),
            ],
            env=self.environment,
            check=True,
            capture_output=True,
            timeout=30,
        )

    def render(self, public_url, port="18181"):
        """Render synthetic selected settings; retain all generated secrets."""
        changes = {
            "CATALOG_PUBLIC_URL": public_url,
            "CATALOG_PORT": port,
        }
        lines = []
        seen = set()
        for line in self.site.read_text().splitlines():
            key = line.partition("=")[0]
            if key in changes:
                line = key + "=" + changes[key]
                seen.add(key)
            lines.append(line)
        self.assertEqual(seen, set(changes))
        self.site.write_text("\n".join(lines) + "\n")
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                str(self.site),
                "-f",
                str(ROOT / "infra/lakehouse/compose.yaml"),
                "config",
                "--format",
                "json",
            ],
            env=self.environment,
            capture_output=True,
            check=True,
            timeout=30,
        )
        return json.loads(result.stdout)["services"]

    def test_public_ui_uses_https_without_redirecting_internal_clients(self):
        services = self.render("https://catalog.example.invalid")
        serving = services["catalog"]["environment"]
        migration = services["catalog-migrate"]["environment"]
        self.assertEqual(
            serving["LAKEKEEPER__UI__LAKEKEEPER_URL"],
            "https://catalog.example.invalid",
        )
        self.assertEqual(
            serving["LAKEKEEPER__BASE_URI"], "http://catalog:8181"
        )
        self.assertEqual(
            migration["LAKEKEEPER__BASE_URI"], "http://catalog:8181"
        )
        self.assertNotIn("LAKEKEEPER__UI__LAKEKEEPER_URL", migration)
        self.assertNotIn("LAKEKEEPER__ALLOW_ORIGIN", serving)
        self.assertEqual(serving["LAKEKEEPER__AUTHZ_BACKEND"], "allowall")

    def test_local_ui_uses_selected_host_port_not_the_internal_dns_name(self):
        services = self.render("")
        serving = services["catalog"]["environment"]
        self.assertEqual(
            serving["LAKEKEEPER__UI__LAKEKEEPER_URL"],
            "http://localhost:18181",
        )
        self.assertEqual(services["catalog"]["ports"][0]["published"], "18181")
        self.assertEqual(
            serving["LAKEKEEPER__BASE_URI"], "http://catalog:8181"
        )
