"""Static separate-manager deployment contracts, not live Docker acceptance."""

import configparser
import shlex
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).absolute().parents[1]


class ManagementComposeTests(unittest.TestCase):
    def setUp(self):
        self.source = ROOT / "infra/management/compose.yaml"
        self.config = yaml.safe_load(self.source.read_text())
        self.service = self.config["services"]["poc-manager"]

    def test_manager_is_a_separate_non_profiled_project(self):
        self.assertEqual(set(self.config["services"]), {"poc-manager"})
        self.assertNotIn("profiles", self.service)
        self.assertEqual(self.service["restart"], "unless-stopped")
        self.assertIn("lakehouse-manager", self.config["name"])

    def test_web_container_has_no_docker_or_host_authority(self):
        self.assertEqual(self.service["user"], "10001:10001")
        self.assertTrue(self.service["read_only"])
        self.assertEqual(self.service["cap_drop"], ["ALL"])
        self.assertEqual(
            self.service["security_opt"], ["no-new-privileges:true"]
        )
        self.assertNotIn("privileged", self.service)
        self.assertNotIn("network_mode", self.service)
        self.assertNotIn("devices", self.service)
        for item in self.service["volumes"]:
            self.assertIn(item["target"], {"/state", "/run/manager-secrets"})
            self.assertFalse(item["bind"]["create_host_path"])
            self.assertNotIn("docker.sock", item["source"])
        self.assertTrue(self.service["volumes"][1]["read_only"])
        self.assertEqual(
            self.service["ports"], ["127.0.0.1:${MANAGER_PORT:-8010}:8000"]
        )

    def test_healthcheck_and_runtime_do_not_read_credentials_or_proxy(self):
        command = self.service["healthcheck"]["test"][-1]
        self.assertIn("/health", command)
        self.assertIn("MANAGER_PUBLIC_ORIGIN", command)
        self.assertIn("ProxyHandler({})", command)
        self.assertNotIn("token", command)
        self.assertFalse(
            {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
            & self.service["environment"].keys()
        )
        self.assertEqual(self.service["mem_limit"], "384m")
        self.assertEqual(self.service["logging"]["driver"], "json-file")

    def test_local_image_builder_and_existing_official_pins(self):
        self.assertEqual(self.service["pull_policy"], "build")
        build = self.service["build"]
        self.assertEqual(build["context"], "../..")
        self.assertEqual(build["dockerfile"], "infra/management/Dockerfile")
        source = (ROOT / build["dockerfile"]).read_text()
        existing = (ROOT / "infra/demo/Dockerfile").read_text()
        for line in source.splitlines():
            if line.startswith("FROM ") and "@sha256:" in line:
                self.assertIn(line.split(" AS ")[0], existing)
        self.assertIn("--require-hashes", source)
        self.assertIn("fetch_swagger.py", source)
        self.assertIn("USER 10001:10001", source)
        self.assertNotIn("docker.sock", source)

    def test_optional_access_proxy_overlay_preserves_six_names(self):
        overlay = yaml.safe_load(
            (ROOT / "infra/management/compose.proxy.yaml.example").read_text()
        )["services"]["poc-manager"]["environment"]
        for upper, lower in (
            ("HTTP_PROXY", "http_proxy"),
            ("HTTPS_PROXY", "https_proxy"),
            ("NO_PROXY", "no_proxy"),
        ):
            self.assertEqual(overlay[upper], overlay[lower])
        self.assertIn("LAKEHOUSE_NO_PROXY", overlay["NO_PROXY"])
        self.assertIn("poc-manager", overlay["NO_PROXY"])

    def test_runner_unit_cannot_restart_after_denial_or_start_docker(self):
        unit = configparser.ConfigParser(interpolation=None)
        unit.read(ROOT / "infra/management/poc-manager-runner.service.example")
        self.assertEqual(unit["Service"]["Restart"], "no")
        self.assertEqual(unit["Service"]["User"], "root")
        self.assertNotIn("Requires", unit["Unit"])
        self.assertNotIn("Wants", unit["Unit"])
        command = shlex.split(unit["Service"]["ExecStart"])
        self.assertIn("serve", command)
        self.assertIn("--manifest", command)
        self.assertIn("--public-origin", command)
        self.assertEqual(
            command[command.index("--origin") + 1],
            "http://127.0.0.1:8010",
        )
        self.assertEqual(
            unit["Service"]["ReadWritePaths"],
            "/var/lib/lakehouse-poc-manager/runner-intent",
        )


if __name__ == "__main__":
    unittest.main()
