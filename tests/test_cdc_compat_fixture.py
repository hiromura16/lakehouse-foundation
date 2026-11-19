"""Pure fixture parsing/guards: no Docker, SQL, network or site settings."""

import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
