"""Test authenticated CDC queue/cache semantics without binding a TCP port."""

import io
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import runtime_control  # noqa: E402
from cdc_fixtures import MAPPING  # noqa: E402


class ControlTests(unittest.TestCase):
    """Acceptance is a queued request, never a materialization success."""

    def setUp(self):
        self.token = "t" * 64
        self.control = runtime_control.Control(
            "fixed-demo", self.token, MAPPING
        )
        self.auth = "Bearer " + self.token

    def test_status_requires_auth_and_never_contains_authentication(self):
        status, body = self.control.dispatch("GET", "/status", "")
        self.assertEqual(status, 401)
        status, body = self.control.dispatch("GET", "/status", self.auth)
        self.assertEqual(status, 200)
        self.assertFalse(body["ready"])
        self.assertIsNone(body["observation_time"])
        self.assertNotIn(self.token, json.dumps(body))
        self.assertEqual(set(body["current_rows"]), set(MAPPING))
        body["current_rows"]["erp_products"] = 999
        self.assertIsNone(
            self.control.snapshot()["current_rows"]["erp_products"]
        )

    def test_rebuild_is_202_queued_then_only_main_thread_claims_it(self):
        status, body = self.control.dispatch(
            "POST", "/rebuild", self.auth, b'{"confirm":true}'
        )
        self.assertEqual(status, 202)
        self.assertRegex(body["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(body["status"], "queued")
        self.assertFalse(body["completed"])
        self.assertEqual(
            self.control.snapshot()["rebuild"]["status"], "queued"
        )
        self.assertEqual(self.control.take_rebuild(), body["request_id"])
        self.assertIsNone(self.control.take_rebuild())
        self.control.finish_rebuild(
            body["request_id"], succeeded=True, snapshot_id=42
        )
        result = self.control.snapshot()["rebuild"]
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["completed"])
        self.assertEqual(result["raw_snapshot_id"], 42)

    def test_strict_consent_rejects_arrays_duplicates_numbers_and_extra_fields(
        self,
    ):
        for body in (
            b"{}",
            b'{"confirm":false}',
            b'{"confirm":1}',
            b'{"confirm":"true"}',
            b'{"confirm":true,"confirm":true}',
            b'[["confirm",true]]',
            b'{"confirm":true,"sql":"DROP TABLE"}',
            b"null",
            b"{bad",
            b" " * 4097,
        ):
            with self.subTest(length=len(body)):
                status, _ = self.control.dispatch(
                    "POST", "/rebuild", self.auth, body
                )
                self.assertEqual(status, 400)
                self.assertIsNone(self.control.snapshot()["rebuild"])

    def test_pending_requests_generic_mode_and_query_blockers_are_refused(
        self,
    ):
        self.control.update(blocked=True)
        status, body = self.control.dispatch(
            "POST", "/rebuild", self.auth, b'{"confirm":true}'
        )
        self.assertEqual(
            (status, body["error_code"]), (409, "CDC_QUERY_REVIEW_REQUIRED")
        )
        self.control.update(blocked=False)
        self.control.dispatch(
            "POST", "/rebuild", self.auth, b'{"confirm":true}'
        )
        self.assertEqual(
            self.control.dispatch(
                "POST", "/rebuild", self.auth, b'{"confirm":true}'
            )[0],
            409,
        )
        generic = runtime_control.Control("generic", self.token, MAPPING)
        self.assertEqual(
            generic.dispatch(
                "POST", "/rebuild", self.auth, b'{"confirm":true}'
            )[0],
            409,
        )

    def test_unknown_status_fields_and_invalid_unicode_auth_are_safe(self):
        with self.assertRaises(ValueError):
            self.control.update(password="unit-secret")
        status, _ = self.control.dispatch("GET", "/status", "秘密")
        self.assertEqual(status, 401)
        self.assertEqual(
            self.control.dispatch("POST", "/other", self.auth)[0], 404
        )

    def test_http_adapter_authenticates_before_body_and_sets_read_deadline(
        self,
    ):
        captured = {}

        class Server:
            """Capture the handler class without opening a real listener."""

            def __init__(self, address, handler):
                captured.update(address=address, handler=handler)

            def serve_forever(self):
                """The mocked thread never starts this function."""
                raise AssertionError("No server may run in this unit test.")

        with (
            mock.patch.object(runtime_control, "ThreadingHTTPServer", Server),
            mock.patch.object(runtime_control.threading, "Thread"),
        ):
            runtime_control.start_server(self.control)
        self.assertEqual(captured["address"], ("0.0.0.0", 8001))
        handler = object.__new__(captured["handler"])
        handler.request = SimpleNamespace(settimeout=mock.Mock())
        with mock.patch.object(
            runtime_control.BaseHTTPRequestHandler, "setup"
        ):
            handler.setup()
        handler.request.settimeout.assert_called_once_with(5)
        handler.headers = {"Content-Length": "999999999"}
        handler.rfile = SimpleNamespace(
            read=mock.Mock(
                side_effect=AssertionError("Unauthenticated body was read.")
            )
        )
        handler.wfile = io.BytesIO()
        handler.command, handler.path = "POST", "/rebuild"
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.handle_request()
        handler.send_response.assert_called_once_with(401)
        handler.rfile.read.assert_not_called()
        self.assertNotIn(self.token.encode(), handler.wfile.getvalue())


if __name__ == "__main__":
    unittest.main()
