"""Mock worker HTTP acknowledgements; no network, SQL or Iceberg is used."""

import importlib.util
import sys
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

# Skip only an absent optional package; keep broken installs as errors.
try:
    import httpx
except ModuleNotFoundError as error:
    if error.name != "httpx" or importlib.util.find_spec("httpx") is not None:
        raise
    raise unittest.SkipTest(
        "Optional tests need httpx. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "infra/demo"), *sys.path]):
    SPEC = importlib.util.spec_from_file_location(
        "sqlsource_client_subject", ROOT / "infra/demo/sqlsource_client.py"
    )
    CLIENT = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(CLIENT)
BATCH_ID = "d" * 64
STAMP = "2026-01-01T00:00:00.1234567Z"


def acknowledgement():
    """Describe a completed synthetic generation with all six counts."""
    return {
        "completed": True,
        "batch_id": BATCH_ID,
        "observed_at": STAMP,
        "apply_id": sha256((BATCH_ID + "\n" + STAMP).encode()).hexdigest(),
        "skipped": False,
        "sql_statements": 14,
        "counts": dict.fromkeys(CLIENT.CONTRACT_IDS, 1),
    }


class SqlSourceClientTests(unittest.TestCase):
    """Do not advance progress on mismatched, partial or unknown outcomes."""

    def response(self, value):
        """Inject a transport with a real JSON-like response interface."""
        response = mock.Mock(status_code=200)
        response.json.return_value = value
        client = mock.Mock()
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.post.return_value = response
        return client

    def test_request_has_only_fixed_identity_and_filters_reply(self):
        client = self.response(
            {**acknowledgement(), "password": "synthetic-private-detail"}
        )
        with mock.patch.object(
            CLIENT.httpx, "Client", return_value=client
        ) as (factory):
            result = CLIENT.send_batch("unit-worker", BATCH_ID, STAMP)
        self.assertNotIn("synthetic-private", str(result))
        factory.assert_called_once_with(
            timeout=240, trust_env=False, follow_redirects=False
        )
        call = client.post.call_args
        self.assertEqual(call.args, ("http://worker:8000/run",))
        self.assertEqual(
            call.kwargs["json"],
            {
                "action": "sql-load",
                "batch_id": BATCH_ID,
                "observed_at": STAMP,
            },
        )
        self.assertEqual(
            call.kwargs["headers"],
            {
                "Authorization": "Bearer unit-worker",
            },
        )

    def test_partial_mismatched_and_invalid_counts_are_not_success(self):
        for change in (
            {"completed": False},
            {"batch_id": "e" * 64},
            {"apply_id": "e" * 64},
            {"observed_at": "invalid"},
            {"counts": {}},
            {"counts": dict.fromkeys(CLIENT.CONTRACT_IDS, True)},
            {"skipped": "true"},
        ):
            with self.subTest(fields=list(change)):
                client = self.response({**acknowledgement(), **change})
                with mock.patch.object(
                    CLIENT.httpx, "Client", return_value=client
                ):
                    with self.assertRaises(CLIENT.OperationFailure):
                        CLIENT.send_batch("unit-worker", BATCH_ID, STAMP)

    def test_timeout_is_unknown_and_driver_text_is_redacted(self):
        client = self.response({})
        client.post.side_effect = httpx.ReadTimeout("synthetic-private-url")
        with mock.patch.object(CLIENT.httpx, "Client", return_value=client):
            with self.assertRaises(CLIENT.OperationFailure) as error:
                CLIENT.send_batch("unit-worker", BATCH_ID, STAMP)
        self.assertEqual(error.exception.code, "SQL_WORKER_RESULT_UNKNOWN")
        self.assertNotIn("synthetic-private", error.exception.message)
        self.assertEqual(client.post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
