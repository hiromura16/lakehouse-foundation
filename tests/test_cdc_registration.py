"""Keep Kafka Connect registration idempotent without changing live state."""

import contextlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
CDC = ROOT / "infra/cdc"
with mock.patch.object(sys, "path", [str(CDC), *sys.path]):
    SPEC = importlib.util.spec_from_file_location(
        "cdc_registration_subject", CDC / "connector.py"
    )
    CONNECTOR = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(CONNECTOR)


class RegistrationTests(unittest.TestCase):
    """Mock only HTTP and local candidate assembly; perform no live calls."""

    def setUp(self):
        self.endpoint = "http://cdc-connect:8083"
        self.name = "factory-source"
        self.candidate = {
            "connector.class": "example.FixedSourceConnector",
            "tasks.max": "1",
            "database.password": "${env:CDC_SQLSERVER_PASSWORD}",
        }
        self.values = {"CDC_CONNECTOR_NAME": self.name}
        self.mapping = {"table": {}}
        self.opener = mock.Mock()
        patched = mock.patch.object(
            CONNECTOR, "build_opener", return_value=self.opener
        )
        patched.start()
        self.addCleanup(patched.stop)
        patched = mock.patch.object(
            CONNECTOR, "configuration", return_value=self.candidate
        )
        patched.start()
        self.addCleanup(patched.stop)
        self.output = io.StringIO()

    def response(self, payload, status=200):
        result = mock.MagicMock()
        body = io.BytesIO(json.dumps(payload).encode())
        body.status = status
        result.__enter__.return_value = body
        return result

    def register(self):
        with contextlib.redirect_stdout(self.output):
            CONNECTOR.register(self.values, self.mapping, self.endpoint)

    def test_identical_existing_config_with_connect_name_is_read_only_success(
        self,
    ):
        self.opener.open.side_effect = [
            self.response([self.name]),
            self.response({**self.candidate, "name": self.name}),
        ]
        self.register()
        self.assertTrue(json.loads(self.output.getvalue())["unchanged"])
        self.assertEqual(
            [call.args[0] for call in self.opener.open.call_args_list],
            [
                self.endpoint + "/connectors",
                self.endpoint + "/connectors/" + self.name + "/config",
            ],
        )
        self.assertNotIn("database.password", self.output.getvalue())
        self.assertNotIn(self.name, self.output.getvalue())

    def test_identical_config_without_synthetic_name_is_also_success(self):
        self.opener.open.side_effect = [
            self.response([self.name]),
            self.response(self.candidate),
        ]
        self.register()
        self.assertTrue(json.loads(self.output.getvalue())["registered"])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_differences_extra_keys_and_wrong_name_are_refused_without_writes(
        self,
    ):
        variants = [
            {**self.candidate, "tasks.max": "2"},
            {**self.candidate, "tasks.max": 1},
            {**self.candidate, "unexpected.default": "true"},
            {**self.candidate, "name": "another-source"},
            {
                key: value
                for key, value in self.candidate.items()
                if key != "tasks.max"
            },
        ]
        for current in variants:
            with self.subTest(current=current):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = [
                    self.response([self.name]),
                    self.response(current),
                ]
                with self.assertRaises(ValueError):
                    self.register()
                self.assertEqual(self.opener.open.call_count, 2)
                self.assertTrue(
                    all(
                        isinstance(call.args[0], str)
                        for call in self.opener.open.call_args_list
                    )
                )
        self.assertEqual(self.output.getvalue(), "")

    def test_auth_and_http_failures_cannot_fall_through_to_create(self):
        for status in (401, 403, 404, 500):
            for readback in (False, True):
                with self.subTest(status=status, readback=readback):
                    failure = HTTPError(
                        self.endpoint,
                        status,
                        "private-server-detail",
                        {},
                        None,
                    )
                    self.opener.open.reset_mock()
                    self.opener.open.side_effect = (
                        [self.response([self.name]), failure]
                        if readback
                        else [failure]
                    )
                    with self.assertRaises((HTTPError, RuntimeError)):
                        self.register()
                    self.assertEqual(
                        self.opener.open.call_count, 2 if readback else 1
                    )
        self.assertEqual(self.output.getvalue(), "")

    def test_malformed_discovery_or_readback_never_creates_a_connector(self):
        for responses in (
            [self.response({"unexpected": "dictionary"})],
            [self.response([self.name]), self.response(["not-a-config"])],
            [self.response([self.name]), self.response({}, status=503)],
        ):
            self.opener.open.reset_mock()
            self.opener.open.side_effect = responses
            with self.assertRaises((ValueError, RuntimeError)):
                self.register()
            self.assertTrue(
                all(
                    isinstance(call.args[0], str)
                    for call in self.opener.open.call_args_list
                )
            )

    def test_only_a_missing_connector_is_created_with_a_single_post(self):
        self.opener.open.side_effect = [
            self.response([]),
            self.response({}, status=201),
        ]
        self.register()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, self.endpoint + "/connectors")
        self.assertEqual(
            json.loads(request.data),
            {
                "name": self.name,
                "config": self.candidate,
            },
        )
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertTrue(json.loads(self.output.getvalue())["registered"])


class AgentQueryTests(unittest.TestCase):
    """DB名を一度だけ引用し、任意SQLを引き続き拒否する。"""

    def candidate(self, query):
        """秘密値のない合成設定から実際のconnector設定を組み立てる。"""
        values = CONNECTOR.settings(
            {
                "CDC_SQLSERVER_HOST": "example.invalid",
                "CDC_SQLSERVER_DATABASE": "demo_database",
                "CDC_SQLSERVER_USER": "synthetic_reader",
                "CDC_SQLSERVER_PASSWORD": "synthetic_value",
                "CDC_SQLSERVER_AGENT_STATUS_QUERY": query,
            }
        )
        return CONNECTOR.configuration(values, {})

    def test_debezium_database_replacement_quotes_the_database_once(self):
        for placeholder in ("[#db]", "[#DB]"):
            with self.subTest(placeholder=placeholder):
                candidate = self.candidate(
                    "SELECT " + placeholder + ".[probe].[is_running]()"
                )
                rendered = candidate["database.sqlserver.agent.status.query"]
                self.assertEqual(
                    rendered.replace("#db", "[demo_database]"),
                    "SELECT [demo_database].[probe].[is_running]()",
                )

    def test_extra_sql_and_arguments_remain_rejected(self):
        for query in (
            "SELECT [#db].[probe].[is_running](); SELECT 1",
            "SELECT [#db].[probe].[is_running](1)",
            "SELECT 1",
            "SELECT [other].[probe].[is_running]()",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.candidate(query)


if __name__ == "__main__":
    unittest.main()
