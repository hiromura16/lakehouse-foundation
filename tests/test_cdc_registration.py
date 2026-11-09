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


if __name__ == "__main__":
    unittest.main()
