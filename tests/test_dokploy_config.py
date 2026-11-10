"""Test Dokploy configuration without Docker access or host changes."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra/dokploy/install.sh"
SPEC = importlib.util.spec_from_file_location(
    "dokploy_config", ROOT / "infra/dokploy/config.py"
)
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)
# Synthetic inputs below are not deployment endpoints.
PRIVATE_HOST = ".".join(str(octet) for octet in (10, 50, 0, 20))
EXTERNAL_HOST = ".".join(str(octet) for octet in (203, 0, 113, 20))
OVERLAP_HOST = ".".join(str(octet) for octet in (172, 30, 1, 2))
BASE = f"""DOKPLOY_BIND_IP={PRIVATE_HOST}
DOKPLOY_ALLOWED_CIDRS=10.50.0.0/24,127.0.0.1/32
DOKPLOY_ADMIN_PORT=3100
SWARM_ADVERTISE_ADDR={PRIVATE_HOST}
SWARM_ADDRESS_POOL=172.30.0.0/16
SWARM_SUBNET_SIZE=24
DOCKER_BRIDGE_CIDR=
SOURCE_SQLSERVER_PASSWORD=synthetic-not-for-output
"""


class DokployConfigurationTests(unittest.TestCase):
    """Check the wrapper plan and validators with local fixtures."""

    def test_fifteen_dry_plan_cases_never_call_host_tools(self):
        cases = [
            ("default plan", BASE, 0o600, True),
            (
                "loopback bind",
                BASE.replace(
                    f"DOKPLOY_BIND_IP={PRIVATE_HOST}",
                    "DOKPLOY_BIND_IP=127.0.0.1",
                ),
                0o600,
                True,
            ),
            (
                "explicit bridge",
                BASE.replace(
                    "DOCKER_BRIDGE_CIDR=", "DOCKER_BRIDGE_CIDR=172.18.0.0/16"
                ),
                0o600,
                True,
            ),
            (
                "public bind",
                BASE.replace(
                    f"DOKPLOY_BIND_IP={PRIVATE_HOST}",
                    f"DOKPLOY_BIND_IP={EXTERNAL_HOST}",
                ),
                0o600,
                False,
            ),
            (
                "broad clients",
                BASE.replace("10.50.0.0/24,127.0.0.1/32", "0.0.0.0/0"),
                0o600,
                False,
            ),
            (
                "bridge overlap",
                BASE.replace(
                    "DOCKER_BRIDGE_CIDR=", "DOCKER_BRIDGE_CIDR=172.30.1.0/24"
                ),
                0o600,
                False,
            ),
            (
                "shell syntax",
                BASE.replace(
                    f"SWARM_ADVERTISE_ADDR={PRIVATE_HOST}",
                    "SWARM_ADVERTISE_ADDR=$(do-not-execute)",
                ),
                0o600,
                False,
            ),
            (
                "duplicate key",
                BASE + "DOKPLOY_ADMIN_PORT=3200\n",
                0o600,
                False,
            ),
            (
                "missing advertise",
                BASE.replace(
                    f"SWARM_ADVERTISE_ADDR={PRIVATE_HOST}",
                    "SWARM_ADVERTISE_ADDR=",
                ),
                0o600,
                False,
            ),
            (
                "reserved Swarm port",
                BASE.replace(
                    "DOKPLOY_ADMIN_PORT=3100", "DOKPLOY_ADMIN_PORT=2377"
                ),
                0o600,
                False,
            ),
            ("non-private env", BASE, 0o644, False),
            ("missing env argument", None, 0o600, False),
            (
                "lowercase proxy",
                BASE
                + "http_proxy=http://synthetic-not-for-output@proxy:3128\n",
                0o600,
                True,
            ),
            (
                "public advertise",
                BASE.replace(
                    f"SWARM_ADVERTISE_ADDR={PRIVATE_HOST}",
                    f"SWARM_ADVERTISE_ADDR={EXTERNAL_HOST}",
                ),
                0o600,
                False,
            ),
            (
                "IPv6 bind",
                BASE.replace(
                    f"DOKPLOY_BIND_IP={PRIVATE_HOST}", "DOKPLOY_BIND_IP=::1"
                ),
                0o600,
                False,
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            binary = directory / "bin"
            binary.mkdir()
            called = directory / "called"
            for name in ("docker", "systemctl", "ip", "ss", "curl"):
                command = binary / name
                command.write_text(
                    "#!/bin/sh\nprintf called >> '"
                    + str(called)
                    + "'\nexit 99\n"
                )
                command.chmod(0o700)
            environment = dict(os.environ)
            environment["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
            for name, contents, mode, succeeds in cases:
                with self.subTest(name=name):
                    arguments = ["bash", str(SCRIPT)]
                    if contents is not None:
                        path = directory / "site.env"
                        path.write_text(contents)
                        path.chmod(mode)
                        arguments += ["--env-file", str(path)]
                    result = subprocess.run(
                        arguments,
                        capture_output=True,
                        text=True,
                        env=environment,
                        check=False,
                    )
                    self.assertEqual(result.returncode == 0, succeeds)
                    self.assertNotIn(
                        "synthetic-not-for-output",
                        result.stdout + result.stderr,
                    )
                    self.assertFalse(called.exists())

    def test_private_port_bindings_and_loopback_dedup(self):
        CONFIG.validate_ports(
            {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "3100"}]},
            "gateway",
            "3100",
            "127.0.0.1",
            ["127.0.0.1"],
        )
        with self.assertRaises(ValueError):
            CONFIG.validate_ports(
                {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "3100"}]},
                "gateway",
                "3100",
                "127.0.0.1",
                ["127.0.0.1"],
            )

    def test_swarm_allocation_must_match(self):
        CONFIG.validate_swarm(
            {"DefaultAddrPool": ["172.30.0.0/16"], "SubnetSize": 24},
            "172.30.0.0/16",
            "24",
        )
        with self.assertRaises(ValueError):
            CONFIG.validate_swarm(
                {"DefaultAddrPool": ["172.30.0.0/16"], "SubnetSize": 24},
                "172.20.0.0/16",
                "24",
            )

    def test_docker_info_cluster_json_preserves_allocation_validation(self):
        """Accept manager Cluster JSON and reject drift or the outer Swarm."""
        cluster = {
            "ID": "synthetic-cluster",
            "DefaultAddrPool": ["172.30.0.0/16"],
            "SubnetSize": 24,
            "Spec": {"Name": "synthetic-swarm"},
        }
        payloads = [
            ("matching cluster", cluster, True),
            ("missing cluster", None, False),
            (
                "different pool",
                {**cluster, "DefaultAddrPool": ["172.20.0.0/16"]},
                False,
            ),
            ("different subnet", {**cluster, "SubnetSize": 25}, False),
            (
                "outer Swarm object",
                {"LocalNodeState": "active", "Cluster": cluster},
                False,
            ),
            ("missing allocation", {"ID": "synthetic-cluster"}, False),
        ]
        for name, payload, succeeds in payloads:
            with self.subTest(name=name):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "infra/dokploy/config.py"),
                        "swarm",
                        "172.30.0.0/16",
                        "24",
                    ],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode == 0, succeeds)
                if not succeeds:
                    self.assertIn("Existing Swarm pool differs", result.stderr)

    def test_installer_cluster_pipeline_uses_only_docker_info(self):
        """Run the real read-only pipeline against a strict Docker 29 stub."""
        lines = SCRIPT.read_text().splitlines()
        index = next(
            number
            for number, line in enumerate(lines)
            if 'config.py" swarm' in line
        )
        start = index - 1
        stop = index + 1
        pipeline = "\n".join(lines[start:stop])
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / "docker"
            command.write_text(
                "#!" + sys.executable + "\n"
                "import os, sys\n"
                "if sys.argv[1:] != "
                "['info', '--format', '{{json .Swarm.Cluster}}']:\n"
                "    raise SystemExit(91)\n"
                "print(os.environ['TEST_CLUSTER_JSON'])\n"
            )
            command.chmod(0o700)
            environment = dict(os.environ)
            environment.update(
                PATH=directory + os.pathsep + os.environ["PATH"],
                SCRIPT_DIR=str(SCRIPT.parent),
                ADDRESS_POOL="172.30.0.0/16",
                SUBNET_SIZE="24",
            )
            for subnet, succeeds in ((24, True), (25, False)):
                with self.subTest(subnet=subnet):
                    environment["TEST_CLUSTER_JSON"] = json.dumps(
                        {
                            "DefaultAddrPool": ["172.30.0.0/16"],
                            "SubnetSize": subnet,
                        }
                    )
                    result = subprocess.run(
                        ["bash", "-o", "pipefail", "-c", pipeline],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode == 0, succeeds)

    def test_host_network_overlap_is_rejected(self):
        interfaces = [
            {"addr_info": [{"local": OVERLAP_HOST, "prefixlen": 24}]}
        ]
        with self.assertRaises(ValueError):
            CONFIG.validate_host_networks(interfaces, "172.30.0.0/16")

    def test_detected_docker_bridge_is_narrow_and_checked(self):
        network = [{"Subnet": "172.18.0.0/16"}]
        self.assertEqual(
            CONFIG.inspect_bridge(network, "172.30.0.0/16", ""),
            "172.18.0.0/16",
        )
        with self.assertRaises(ValueError):
            CONFIG.inspect_bridge(network, "172.30.0.0/16", "172.19.0.0/16")

    def test_nginx_render_preserves_only_explicit_networks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nginx.conf"
            CONFIG.render_nginx(
                output, "10.50.0.0/24", "172.30.0.0/16", "172.18.0.0/16"
            )
            text = output.read_text()
            for network in (
                "127.0.0.0/8",
                "10.50.0.0/24",
                "172.30.0.0/16",
                "172.18.0.0/16",
            ):
                self.assertIn("allow " + network + ";", text)
            self.assertNotIn("allow 172.16.0.0/12;", text)
            self.assertIn("deny all;", text)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)


if __name__ == "__main__":
    unittest.main()
