"""Keep the Streamlit server's API token on its configured origin."""

import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "demo_client_subject", ROOT / "infra/demo/client.py"
)
CLIENT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLIENT)


class DemoClientTests(unittest.TestCase):
    """Verify authentication, redirect refusal and sanitized failure output."""

    def setUp(self):
        context = mock.patch.dict(
            os.environ,
            {
                "DEMO_API_URL": "http://demo-api:8000",
                "CONTROL_API_TOKEN": "synthetic-api-placeholder",
            },
        )
        context.start()
        self.addCleanup(context.stop)

    def test_server_authentication_and_json_body_are_sent_once(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"id":"demo"}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(CLIENT, "build_opener", return_value=opener):
            result = CLIENT.request(
                "/jobs", method="POST", payload={"mode": "batch"}
            )
        self.assertEqual(result, {"id": "demo"})
        call = opener.open.call_args.args[0]
        self.assertEqual(
            call.get_header("Authorization"),
            "Bearer " + "synthetic-api-placeholder",
        )
        self.assertEqual(json.loads(call.data), {"mode": "batch"})
        self.assertEqual(call.full_url, "http://demo-api:8000/jobs")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        self.assertIsNone(
            CLIENT.NoRedirects().redirect_request(
                None, None, 302, "redirect", {}, "http://other.invalid/"
            )
        )

    def test_credential_bearing_urls_and_external_paths_are_refused(self):
        with mock.patch.object(CLIENT, "build_opener") as opener:
            for path in ["//other.invalid/", "jobs"]:
                with self.assertRaises(ValueError):
                    CLIENT.request(path)
            with mock.patch.dict(
                os.environ, {"DEMO_API_URL": "http://user:secret@demo-api"}
            ):
                with self.assertRaises(ValueError):
                    CLIENT.request("/health")
            opener.assert_not_called()

    def test_ai_context_may_wait_longer_than_job_requests(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"datasets":[]}'
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(CLIENT, "build_opener", return_value=opener):
            CLIENT.request("/ai/context?dataset=all&limit=100", timeout=120)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 120)

    def test_http_error_never_displays_the_raw_body(self):
        opener = mock.Mock()
        opener.open.side_effect = HTTPError(
            "http://demo-api/jobs",
            422,
            "bad",
            {},
            io.BytesIO(b"private-driver-details"),
        )
        with mock.patch.object(CLIENT, "build_opener", return_value=opener):
            with self.assertRaises(ValueError) as caught:
                CLIENT.request("/jobs")
        self.assertIn("HTTP 422", str(caught.exception))
        self.assertNotIn("private-driver-details", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
