"""Pure fixture parsing/guards: no Docker, SQL, network or site settings."""

import contextlib
import copy
import importlib.util
import io
import ipaddress
import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cdc_compat_fixture", ROOT / "tests/integration/cdc_compat/run.py"
)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)


class FixtureContracts(unittest.TestCase):
    """Fail closed on runner identity and malformed broker output."""

    def test_docker_failure_categories_never_copy_input(self):
        samples = {
            "manifest unknown": "DOCKER_IMAGE_UNAVAILABLE",
            "no matching manifest": "DOCKER_PLATFORM_UNAVAILABLE",
            "no public port": "DOCKER_PORT_UNAVAILABLE",
            "port is already allocated": "DOCKER_PORT_UNAVAILABLE",
            "container name is already in use": "DOCKER_NAME_CONFLICT",
            "network not found": "DOCKER_NETWORK_UNAVAILABLE",
            "no space left on device": "DOCKER_RESOURCE_EXHAUSTED",
            "cannot connect to the Docker daemon": "DOCKER_DAEMON_UNAVAILABLE",
            "unrecognized private detail": "COMMAND_FAILED",
            "container name only": "COMMAND_FAILED",
        }
        for text, expected in samples.items():
            with self.subTest(expected=expected):
                actual = FIXTURE.docker_failure(text + " PRIVATE_SENTINEL")
                self.assertEqual(actual, expected)
                report = FIXTURE.failure_report(
                    "sql_address", FIXTURE.FixtureError(actual)
                )
                self.assertEqual(report["stage"], "sql_address")
                self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))

    def test_command_classifies_only_failed_required_docker(self):
        result = SimpleNamespace(
            returncode=1,
            stdout="PRIVATE_SENTINEL",
            stderr="no public port PRIVATE_SENTINEL",
        )
        with mock.patch.object(FIXTURE.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(
                FIXTURE.FixtureError, "^DOCKER_PORT_UNAVAILABLE$"
            ):
                FIXTURE.command(["docker", "port"])
            with self.assertRaisesRegex(
                FIXTURE.FixtureError, "^COMMAND_FAILED$"
            ):
                FIXTURE.command(["other"])
            self.assertIs(
                FIXTURE.command(["docker", "port"], optional=True), result
            )
            result.returncode = 0
            self.assertIs(FIXTURE.command(["docker", "port"]), result)

    def test_owned_internal_address_fails_closed(self):
        fixture = object.__new__(FIXTURE.Fixture)
        fixture.prefix = "fixture"
        fixture.network = "fixture-net"
        fixture.names = {"sql": "fixture-sql"}
        # Derive synthetic hosts from a standard private network constant.
        # These are test-only allocations, not site or observed addresses.
        subnet = ipaddress.ip_network("172.16.0.0/12")
        synthetic_ip = str(next(subnet.hosts()))
        synthetic_cidr = synthetic_ip + "/" + str(subnet.prefixlen)
        container = {
            "Id": "container-id",
            "Config": {"Labels": {FIXTURE.LABEL: "fixture"}},
            "HostConfig": {"PortBindings": {}},
            "NetworkSettings": {
                "Networks": {
                    "fixture-net": {
                        "IPAddress": synthetic_ip,
                        "IPPrefixLen": subnet.prefixlen,
                        "NetworkID": "network-id",
                    }
                }
            },
        }
        network = {
            "Id": "network-id",
            "Name": "fixture-net",
            "Driver": "bridge",
            "Internal": True,
            "Labels": {FIXTURE.LABEL: "fixture"},
            "IPAM": {"Config": [{"Subnet": str(subnet)}]},
            "Containers": {"container-id": {"IPv4Address": synthetic_cidr}},
        }

        def address(c, n):
            fixture.docker = mock.Mock(
                side_effect=[
                    SimpleNamespace(stdout=json.dumps([c])),
                    SimpleNamespace(stdout=json.dumps([n])),
                ]
            )
            return fixture.address("sql")

        self.assertEqual(address(container, network), synthetic_ip)
        for field, value in [
            ("Internal", False),
            ("Labels", {}),
            ("Driver", "host"),
            ("Containers", {}),
            ("Id", "other"),
            ("IPAM", {"Config": [{"Subnet": "192.168.0.0/16"}]}),
        ]:
            modified = dict(network, **{field: value})
            with self.assertRaises(FIXTURE.FixtureError):
                address(container, modified)
        for ip in ["127.0.0.1", "invalid"]:
            modified = copy.deepcopy(container)
            modified["NetworkSettings"]["Networks"]["fixture-net"][
                "IPAddress"
            ] = ip
            with self.assertRaises(FIXTURE.FixtureError):
                address(modified, network)
        # Exercise classification failures without storing real/public hosts.
        for flag, value in [
            ("is_private", False),
            ("is_link_local", True),
            ("is_unspecified", True),
        ]:
            fake = mock.Mock(
                is_private=True,
                is_loopback=False,
                is_link_local=False,
                is_unspecified=False,
            )
            setattr(fake, flag, value)
            with mock.patch.object(
                FIXTURE.ipaddress, "IPv4Address", return_value=fake
            ):
                with self.assertRaises(FIXTURE.FixtureError):
                    address(container, network)
        modified = copy.deepcopy(container)
        modified["NetworkSettings"]["Networks"]["other"] = {}
        with self.assertRaises(FIXTURE.FixtureError):
            address(modified, network)
        modified = copy.deepcopy(container)
        modified["HostConfig"]["PortBindings"] = {"1433/tcp": [{}]}
        with self.assertRaises(FIXTURE.FixtureError):
            address(modified, network)

    def test_start_never_publishes_ports(self):
        fixture = object.__new__(FIXTURE.Fixture)
        fixture.prefix = "fixture"
        fixture.network = "fixture-net"
        fixture.names = {"sql": "fixture-sql"}
        fixture.docker = mock.Mock()
        fixture.start("sql", "synthetic-image", {}, alias="fixture-sql")
        args = fixture.docker.call_args.args
        self.assertNotIn("--publish", args)
        self.assertEqual(args[args.index("--network") + 1], "fixture-net")

    def test_only_hosted_numeric_identity(self):
        environment = {
            "GITHUB_ACTIONS": "true",
            "RUNNER_ENVIRONMENT": "github-hosted",
            "GITHUB_RUN_ID": "123",
            "GITHUB_RUN_ATTEMPT": "1",
        }
        self.assertEqual(FIXTURE.owner(environment), "lh-cdc-compat-123-1")
        for key, value in [
            ("RUNNER_ENVIRONMENT", "self-hosted"),
            ("GITHUB_ACTIONS", "false"),
            ("GITHUB_RUN_ID", "../other"),
            ("GITHUB_RUN_ATTEMPT", ""),
        ]:
            with self.subTest(key=key):
                with self.assertRaises(FIXTURE.FixtureError):
                    FIXTURE.owner(dict(environment, **{key: value}))

    def test_actual_event_fields_and_tombstone(self):
        envelope = {"op": "c", "after": {"id": 2, "value": "synthetic"}}
        text = 'Partition:0\tOffset:4\t{"id":2}\t' + json.dumps(envelope)
        events = FIXTURE.records(
            text + '\nPartition:0\tOffset:5\t{"id":2}\tnull'
        )
        self.assertEqual(events[0]["offset"], 4)
        self.assertIsNone(events[1]["value"])
        self.assertTrue(FIXTURE.observed(events, "c", 2, "synthetic"))
        self.assertFalse(FIXTURE.observed(events, "u", 2, "synthetic"))
        self.assertFalse(FIXTURE.observed(events, "c", 3, "synthetic"))

    def test_malformed_position_is_not_accepted(self):
        for text in ["wrong", "0\t4\t{}\t{}", "Partition:x\tOffset:4\t{}\t{}"]:
            with self.subTest(text=text):
                with self.assertRaises((ValueError, FIXTURE.FixtureError)):
                    FIXTURE.records(text)

    def test_cleanup_refuses_unowned_exact_name(self):
        fixture = FIXTURE.Fixture.__new__(FIXTURE.Fixture)
        fixture.prefix = "lh-cdc-compat-123-1"
        fixture.network = fixture.prefix + "-net"
        fixture.names = {"sql": fixture.prefix + "-sql"}
        fixture.docker = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps([{"Config": {"Labels": {}}, "Labels": {}}]),
            )
        )
        with self.assertRaises(FIXTURE.FixtureError):
            fixture.cleanup()
        self.assertTrue(
            all(
                "inspect" in call.args
                for call in fixture.docker.call_args_list
            )
        )

    def test_cleanup_removes_only_exact_owned_objects(self):
        fixture = FIXTURE.Fixture.__new__(FIXTURE.Fixture)
        fixture.prefix = "lh-cdc-compat-123-1"
        fixture.network = fixture.prefix + "-net"
        fixture.names = {"sql": fixture.prefix + "-sql"}
        labels = {FIXTURE.LABEL: fixture.prefix}
        fixture.docker = mock.Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{"Config": {"Labels": labels}, "Labels": labels}]
                ),
            )
        )
        fixture.cleanup()
        self.assertEqual(
            fixture.docker.call_args_list,
            [
                mock.call(
                    "container", "inspect", fixture.names["sql"], optional=True
                ),
                mock.call(
                    "rm",
                    "--force",
                    "--volumes",
                    fixture.names["sql"],
                    optional=True,
                ),
                mock.call(
                    "network", "inspect", fixture.network, optional=True
                ),
                mock.call("network", "rm", fixture.network, optional=True),
            ],
        )

    def test_resume_requires_committed_source_lsn(self):
        fixture = FIXTURE.Fixture.__new__(FIXTURE.Fixture)
        fixture.get = mock.Mock(
            return_value={
                "offsets": [
                    {"offset": {"commit_lsn": "00000001:00000002:0003"}}
                ]
            }
        )
        self.assertIsNone(fixture.committed("00000001:00000002:0004"))
        self.assertTrue(fixture.committed("00000001:00000002:0003"))

    def test_without_run_argument_no_docker_or_network(self):
        with (
            mock.patch.object(FIXTURE.sys, "argv", ["fixture"]),
            mock.patch.object(FIXTURE, "Fixture") as constructor,
        ):
            self.assertEqual(FIXTURE.main(), 0)
            constructor.assert_not_called()

    def test_restart_rejects_snapshot_even_with_same_final_data(self):
        def event(offset, operation):
            return {"offset": offset, "value": json.dumps({"op": operation})}

        self.assertEqual(
            FIXTURE.resumed_events([event(4, "r"), event(5, "c")], 4),
            [event(5, "c")],
        )
        for events in [
            [event(4, "r")],
            [event(5, "r"), event(6, "c")],
            [event(6, "c")],
        ]:
            with self.subTest(events=events):
                with self.assertRaises(FIXTURE.FixtureError):
                    FIXTURE.resumed_events(events, 4)

    def test_cleanup_inspect_error_is_not_assumed_absence(self):
        fixture = FIXTURE.Fixture.__new__(FIXTURE.Fixture)
        fixture.prefix = "lh-cdc-compat-123-1"
        fixture.network = fixture.prefix + "-net"
        fixture.names = {}
        fixture.docker = mock.Mock(
            return_value=SimpleNamespace(returncode=1, stdout="")
        )
        with self.assertRaises(FIXTURE.FixtureError):
            fixture.cleanup()
        fixture.docker.side_effect = [
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        fixture.cleanup()

    def test_failure_reports_never_include_exception_content(self):
        marker = "synthetic-private-payload-do-not-log"
        errors = [
            RuntimeError(marker),
            ValueError(marker),
            OSError(marker),
            ImportError(marker),
            FIXTURE.FixtureError(marker),
            subprocess.TimeoutExpired(marker, 1, output=marker),
        ]
        for error in errors:
            with self.subTest(kind=type(error).__name__):
                report = FIXTURE.failure_report("sql_schema", error)
                self.assertEqual(report["stage"], "sql_schema")
                self.assertEqual(report["status"], "failed")
                self.assertNotIn(marker, json.dumps(report))
        report = FIXTURE.failure_report(
            marker, FIXTURE.FixtureError("READINESS_TIMEOUT")
        )
        self.assertEqual(report["stage"], "bootstrap")
        self.assertEqual(report["reason"], "READINESS_TIMEOUT")
        self.assertNotIn(marker, json.dumps(report))

    def test_failed_run_reports_stage_and_still_cleans_up(self):
        fixture = mock.Mock()
        fixture.stage = "sql_schema"
        fixture.run.side_effect = RuntimeError("synthetic-private-payload")
        output = io.StringIO()
        with (
            mock.patch.object(FIXTURE.sys, "argv", ["fixture", "--run"]),
            mock.patch.object(FIXTURE, "owner", return_value="synthetic"),
            mock.patch.object(FIXTURE, "Fixture", return_value=fixture),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(FIXTURE.main(), 1)
        fixture.cleanup.assert_called_once_with()
        report = json.loads(output.getvalue())
        self.assertEqual(report["stage"], "sql_schema")
        self.assertEqual(report["reason"], "UNEXPECTED_ERROR")
        self.assertNotIn("synthetic-private-payload", output.getvalue())

    def test_stage_entry_is_enumerated_and_not_success(self):
        fixture = FIXTURE.Fixture.__new__(FIXTURE.Fixture)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            fixture.mark("offset_commit")
            with self.assertRaises(ValueError):
                fixture.mark("synthetic-private-payload")
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "started",
                "stage": "offset_commit",
            },
        )


if __name__ == "__main__":
    unittest.main()
