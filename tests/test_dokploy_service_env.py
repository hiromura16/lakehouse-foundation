"""Test Dokploy environment changes using synthetic settings and Docker mocks.

No test starts Docker, creates Swarm state or reads a deployment's site.env.
The in-memory service simulates CLI argument semantics so a repeated update
must become a no-op while unrelated configuration remains untouched.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SERVICE_ID = "a" * 25


def module(name, relative):
    """Load the implementation by file without running its CLI entrypoint."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


CONFIG = module("dokploy_environment_config", "infra/dokploy/config.py")
with mock.patch.dict(sys.modules, {"config": CONFIG}):
    SERVICE = module(
        "dokploy_environment_service", "infra/dokploy/service_env.py"
    )

PROXY = "http://example-user:synthetic-secret@proxy.example.invalid:3128"
PRIVATE_HOST = ".".join(str(octet) for octet in (10, 60, 0, 20))
BASE = f"""DOKPLOY_BIND_IP=127.0.0.1
DOKPLOY_ALLOWED_CIDRS=127.0.0.1/32
DOKPLOY_ADMIN_PORT=3100
SWARM_ADVERTISE_ADDR={PRIVATE_HOST}
SWARM_ADDRESS_POOL=172.30.0.0/16
SWARM_SUBNET_SIZE=24
DOCKER_BRIDGE_CIDR=
SOURCE_SQLSERVER_PASSWORD=synthetic-source-secret
"""


class ServiceEnvironmentTests(unittest.TestCase):
    """Check strict controls, safe values and canonical proxy propagation."""

    def test_six_proxy_names_and_merged_bypass(self):
        values = {
            "HTTP_PROXY": PROXY,
            "HTTPS_PROXY": PROXY,
            "LAKEHOUSE_NO_PROXY": "one.example.invalid,TRINO",
            "NO_PROXY": "two.example.invalid,ONE.example.invalid",
            "no_proxy": "three.example.invalid",
            "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": "1",
        }
        desired = CONFIG.service_environment(values)
        self.assertEqual(set(desired), set(SERVICE.MANAGED_KEYS))
        for upper in ("HTTP_PROXY", "HTTPS_PROXY"):
            self.assertEqual(desired[upper], PROXY)
            self.assertEqual(desired[upper.lower()], PROXY)
        self.assertEqual(desired["NO_PROXY"], desired["no_proxy"])
        bypass = desired["NO_PROXY"].split(",")
        self.assertTrue(
            set(CONFIG.normalize_no_proxy({}).split(",")) <= set(bypass)
        )
        self.assertEqual(
            len(bypass), len({value.casefold() for value in bypass})
        )
        self.assertEqual(
            bypass[-3:],
            [
                "one.example.invalid",
                "two.example.invalid",
                "three.example.invalid",
            ],
        )
        self.assertEqual(desired["BUILDKIT_NO_CLIENT_TOKEN"], "1")
        self.assertNotIn("DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN", desired)
        self.assertNotIn("LAKEHOUSE_NO_PROXY", desired)

    def test_empty_proxy_is_not_unconditionally_injected(self):
        with mock.patch.dict(os.environ, {"HTTP_PROXY": PROXY}):
            desired = CONFIG.service_environment(
                {"DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": "0"}
            )
        self.assertEqual(desired, {})
        only_http = CONFIG.service_environment({"HTTP_PROXY": PROXY})
        self.assertIn("http_proxy", only_http)
        self.assertNotIn("HTTPS_PROXY", only_http)
        self.assertNotIn("https_proxy", only_http)
        self.assertIn("NO_PROXY", only_http)

    def test_uppercase_inputs_override_stale_lowercase(self):
        desired = CONFIG.service_environment(
            {
                "HTTP_PROXY": PROXY,
                "http_proxy": "http://stale.example.invalid:3128",
            }
        )
        self.assertEqual(desired["HTTP_PROXY"], desired["http_proxy"])
        self.assertEqual(desired["HTTP_PROXY"], PROXY)

    def test_flag_is_strict_and_input_not_reflected(self):
        for value in ("", "true", "false", "yes please", "2", " 1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as error:
                    CONFIG.service_environment(
                        {"DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": value}
                    )
                self.assertEqual(
                    str(error.exception),
                    "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN must be 0 or 1.",
                )

    def test_invalid_proxy_error_hides_input(self):
        for value in (
            "https://synthetic-secret",
            PROXY + "?secret=1",
            "http://synthetic-secret@:3128",
        ):
            with self.subTest(kind="malformed proxy"):
                with self.assertRaises(ValueError) as error:
                    CONFIG.service_environment({"HTTP_PROXY": value})
                self.assertNotIn("synthetic-secret", str(error.exception))

    def test_disabled_removes_existing_managed_flag(self):
        desired = CONFIG.service_environment(
            {"DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": "0"}
        )
        arguments = SERVICE.environment_arguments(
            ["BUILDKIT_NO_CLIENT_TOKEN=1", "UNRELATED=keep"], desired
        )
        self.assertEqual(arguments, ["--env-rm", "BUILDKIT_NO_CLIENT_TOKEN"])

    def test_existing_duplicates_are_repaired_once(self):
        current = ["HTTP_PROXY=old", "HTTP_PROXY=old", "UNRELATED=keep"]
        desired = {"HTTP_PROXY": PROXY, "http_proxy": PROXY}
        arguments = SERVICE.environment_arguments(current, desired)
        self.assertEqual(arguments.count("--env-rm"), 1)
        self.assertEqual(arguments.count("--env-add"), 2)
        self.assertNotIn("UNRELATED", arguments)
        self.assertEqual(
            SERVICE.environment_arguments(
                [
                    "UNRELATED=keep",
                    "HTTP_PROXY=" + PROXY,
                    "http_proxy=" + PROXY,
                ],
                desired,
            ),
            [],
        )

    def test_synchronize_updates_once_and_preserves_unmanaged_state(self):
        service = {
            "Spec": {
                "Labels": {"lab.owner": "lakehouse-foundation"},
                "TaskTemplate": {
                    "ContainerSpec": {
                        "Env": [
                            "HTTP_PROXY=old",
                            "HTTP_PROXY=older",
                            "AUTH_FILE=/secret/file",
                            "RELEASE_TAG=v0.31.0",
                        ],
                        "Mounts": [{"Source": "existing-volume"}],
                    }
                },
            }
        }
        updates = []

        def docker(arguments, timeout=15):
            """Model env-rm/add without giving the tests host capabilities."""
            del timeout
            if arguments == ["service", "inspect", "dokploy"]:
                return json.dumps([service])
            self.assertEqual(
                arguments[:3], ["service", "update", "--detach=true"]
            )
            self.assertEqual(arguments[-1], "dokploy")
            updates.append(arguments)
            environment = service["Spec"]["TaskTemplate"]["ContainerSpec"][
                "Env"
            ]
            options = arguments[3:-1]
            for option, value in zip(options[::2], options[1::2], strict=True):
                if option == "--env-rm":
                    environment[:] = [
                        item
                        for item in environment
                        if item.partition("=")[0] != value
                    ]
                else:
                    self.assertEqual(option, "--env-add")
                    environment.append(value)
            return "dokploy\n"

        desired = CONFIG.service_environment({"HTTP_PROXY": PROXY})
        with (
            mock.patch.object(SERVICE, "docker", docker),
            mock.patch.object(SERVICE, "wait_ready") as wait,
        ):
            SERVICE.synchronize(desired)
            SERVICE.synchronize(desired)
        self.assertEqual(len(updates), 1)
        self.assertEqual(wait.call_count, 2)
        container = service["Spec"]["TaskTemplate"]["ContainerSpec"]
        self.assertIn("AUTH_FILE=/secret/file", container["Env"])
        self.assertEqual(container["Mounts"], [{"Source": "existing-volume"}])
        self.assertEqual(
            len(container["Env"]),
            len({item.partition("=")[0] for item in container["Env"]}),
        )

    def test_unmanaged_service_is_refused_without_update(self):
        service = {"Spec": {"Labels": {"lab.owner": "somebody-else"}}}
        with mock.patch.object(
            SERVICE, "docker", return_value=json.dumps([service])
        ) as docker:
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                SERVICE.synchronize({})
        docker.assert_called_once_with(["service", "inspect", "dokploy"])

    def test_cli_failure_does_not_expose_arguments_or_driver_output(self):
        response = SimpleNamespace(returncode=1, stdout=PROXY, stderr=PROXY)
        with mock.patch.object(
            SERVICE.subprocess, "run", return_value=response
        ):
            with self.assertRaises(ValueError) as error:
                SERVICE.docker(["service", "update", "--env-add", PROXY])
        self.assertNotIn(PROXY, str(error.exception))
        self.assertNotIn("synthetic-secret", str(error.exception))

    def test_new_service_receives_only_selected_env_before_the_image(self):
        """Exercise the create dispatcher with argv arrays and no shell."""
        desired = CONFIG.service_environment({"HTTP_PROXY": PROXY})
        arguments = [
            "service_env.py",
            "--env-file",
            "/example/private.env",
            "create",
            "--",
            "service",
            "create",
            "--name",
            "dokploy",
            "example/dokploy@sha256:example",
        ]
        with (
            mock.patch.object(sys, "argv", arguments),
            mock.patch.object(SERVICE, "read_env", return_value={}),
            mock.patch.object(
                SERVICE, "service_environment", return_value=desired
            ),
            mock.patch.object(SERVICE, "docker") as docker,
            mock.patch.object(SERVICE, "wait_ready") as wait,
        ):
            SERVICE.main()
        selected = docker.call_args.args[0]
        self.assertEqual(selected[-1], "example/dokploy@sha256:example")
        self.assertEqual(
            selected[:4], ["service", "create", "--name", "dokploy"]
        )
        rendered = selected[4:-1]
        self.assertEqual(rendered[::2], ["--env"] * len(desired))
        self.assertEqual(
            set(rendered[1::2]),
            {key + "=" + value for key, value in desired.items()},
        )
        self.assertFalse(
            any(item.startswith("HTTPS_PROXY=") for item in rendered)
        )
        wait.assert_called_once()

    def test_ready_service_filter_uses_full_id_not_name_regex(self):
        """Model Docker prefix filters with a similarly named sibling."""
        services = [
            {"ID": SERVICE_ID, "Name": "dokploy", "Replicas": "1/1"},
            {"ID": "b" * 25, "Name": "dokploy-postgres", "Replicas": "0/1"},
        ]
        now = [0.0]
        filters = []

        def docker(arguments, timeout=15):
            """Match literal prefixes, as Docker's service filters do."""
            del timeout
            if arguments[:2] == ["service", "inspect"]:
                return json.dumps([{"ID": SERVICE_ID}])
            if arguments[:2] == ["service", "ls"]:
                selected = arguments[arguments.index("--filter") + 1]
                filters.append(selected)
                key, prefix = selected.split("=", 1)
                field = {"id": "ID", "name": "Name"}[key]
                return "\n".join(
                    row["Replicas"]
                    for row in services
                    if row[field].startswith(prefix)
                )
            self.assertEqual(arguments[:2], ["service", "ps"])
            return json.dumps({"CurrentState": "Running 1 minute ago"})

        def sleep(seconds):
            """Advance the fake deadline on a regression, never real time."""
            now[0] += seconds

        with mock.patch.object(SERVICE, "docker", docker):
            SERVICE.wait_ready(timeout=4, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(filters, ["id=" + SERVICE_ID])
        self.assertEqual(now[0], 0)

    def test_paused_update_stops_with_safe_diagnostics(self):
        """A paused rollout is a failure even when the previous task runs."""
        replies = [
            json.dumps(
                [{"ID": SERVICE_ID, "UpdateStatus": {"State": "paused"}}]
            ),
            "1/1\n",
            json.dumps(
                {"CurrentState": "Running 1 minute ago", "Error": PROXY}
            ),
        ]
        with mock.patch.object(SERVICE, "docker", side_effect=replies):
            with self.assertRaises(ValueError) as error:
                SERVICE.wait_ready(timeout=120)
        self.assertIn("replicas=1/1", str(error.exception))
        self.assertIn("task_error=SET", str(error.exception))
        self.assertNotIn(PROXY, str(error.exception))

    def test_old_running_task_waits_for_service_update_completion(self):
        now = [0.0]
        observed = []

        def docker(arguments, timeout=15):
            """Keep replicas healthy while simulating a pending new spec."""
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 10)
            if arguments[:2] == ["service", "inspect"]:
                observed.append(now[0])
                state = "updating" if now[0] < 4 else "completed"
                return json.dumps(
                    [{"ID": SERVICE_ID, "UpdateStatus": {"State": state}}]
                )
            if arguments[:2] == ["service", "ls"]:
                return "1/1\n"
            return json.dumps(
                {"CurrentState": "Running 1 minute ago", "Error": ""}
            )

        def sleep(seconds):
            """Advance only the synthetic clock; never block real execution."""
            now[0] += seconds

        with mock.patch.object(SERVICE, "docker", docker):
            SERVICE.wait_ready(timeout=120, clock=lambda: now[0], sleep=sleep)
        self.assertEqual(observed, [0, 2, 4])

    def test_convergence_deadline_hides_untrusted_task_error(self):
        now = [0.0]

        def docker(arguments, timeout=15):
            """Return a stalled synthetic task with a private-looking error."""
            del timeout
            if arguments[:2] == ["service", "inspect"]:
                return json.dumps(
                    [{"ID": SERVICE_ID, "UpdateStatus": {"State": "updating"}}]
                )
            if arguments[:2] == ["service", "ls"]:
                return "0/1\n"
            return json.dumps(
                {"CurrentState": "Rejected 1 second ago", "Error": PROXY}
            )

        def sleep(seconds):
            """Advance the bounded clock without sleeping or Docker access."""
            now[0] += seconds

        with mock.patch.object(SERVICE, "docker", docker):
            with self.assertRaises(ValueError) as error:
                SERVICE.wait_ready(
                    timeout=120, clock=lambda: now[0], sleep=sleep
                )
        self.assertEqual(now[0], 120)
        self.assertIn("replicas=0/1", str(error.exception))
        self.assertIn("Rejected", str(error.exception))
        self.assertIn("task_error=SET", str(error.exception))
        self.assertNotIn(PROXY, str(error.exception))

    def test_dry_run_is_exactly_two_lines_and_never_calls_host_tools(self):
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            private = directory / "site.env"
            private.write_text(BASE + "HTTP_PROXY=" + PROXY + "\n")
            private.chmod(0o600)
            before = private.read_bytes()
            bindir = directory / "bin"
            bindir.mkdir()
            called = directory / "host-tool-called"
            for command in (
                "docker",
                "ip",
                "ss",
                "systemctl",
                "curl",
                "install",
            ):
                path = bindir / command
                path.write_text(
                    "#!/bin/sh\ntouch '" + str(called) + "'\nexit 99\n"
                )
                path.chmod(0o700)
            environment = dict(
                os.environ, PATH=str(bindir) + ":" + os.environ["PATH"]
            )
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "infra/dokploy/install.sh"),
                    "--env-file",
                    str(private),
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(
                result.stdout.splitlines(),
                [
                    "Dokploy service proxy: SET",
                    "Dokploy BuildKit client token mode: enabled",
                ],
            )
            self.assertFalse(called.exists())
            self.assertEqual(private.read_bytes(), before)
            self.assertNotIn(PROXY, result.stdout)
            self.assertNotIn(PRIVATE_HOST, result.stdout)


if __name__ == "__main__":
    unittest.main()
