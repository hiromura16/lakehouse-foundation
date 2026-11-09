"""Check query cancellation and bounded markers without runtime services."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
NEXT_URI = "http://trino:8080/v1/statement/executing/test/1"


def load_worker():
    """Import the real worker while replacing unused service dependencies."""
    names = [
        "boto3",
        "pyarrow",
        "requests",
        "botocore",
        "botocore.config",
        "botocore.exceptions",
        "PIL",
        "pyiceberg",
        "pyiceberg.catalog",
        "pyiceberg.expressions",
    ]
    modules = {name: types.ModuleType(name) for name in names}
    modules["boto3"].client = mock.Mock()
    modules["requests"].Session = mock.Mock()
    modules["botocore.config"].Config = mock.Mock()
    modules["botocore.exceptions"].ClientError = type(
        "ClientError", (Exception,), {}
    )
    modules["PIL"].Image = mock.Mock()
    # Verification hashes a saved image before exercising query cleanup.
    # These protocol tests need a local byte fixture, not the Pillow codec.
    modules["PIL"].Image.new.return_value.save.side_effect = lambda path: (
        path.write_bytes(b"synthetic-test-image")
    )
    modules["pyiceberg.catalog"].load_catalog = mock.Mock()
    modules["pyiceberg.expressions"].EqualTo = mock.Mock()
    specification = importlib.util.spec_from_file_location(
        "worker_protocol_subject", ROOT / "scripts/admin.py"
    )
    worker = importlib.util.module_from_spec(specification)
    with (
        mock.patch.dict(sys.modules, modules),
        mock.patch.dict(
            os.environ,
            {"S3_ACCESS_KEY": "synthetic", "S3_SECRET_KEY": "synthetic"},
        ),
        mock.patch.object(Path, "read_text", return_value='{"sources": []}'),
    ):
        specification.loader.exec_module(worker)
    return worker


WORKER = load_worker()


def response(payload=None, status=200):
    """Return a protocol response with no network or driver implementation."""
    result = mock.Mock(status_code=status)
    result.json.return_value = {} if payload is None else payload
    return result


class QueryProtocolTests(unittest.TestCase):
    """Exercise query lifecycle decisions, including uncertain cancellation."""

    def setUp(self):
        self.state = mock.patch.dict(
            WORKER.STATE,
            {
                "ready": True,
                "blocked": False,
                "successful_runs": 0,
                "failed_runs": 0,
                "last_success": 0,
                "imported_files": 0,
            },
            clear=True,
        )
        self.state.start()
        self.addCleanup(self.state.stop)
        self.session = mock.Mock()
        self.session.post.return_value = response({"nextUri": NEXT_URI})
        self.session.delete.return_value = response(status=204)
        self.http = mock.patch.object(WORKER, "HTTP", self.session)
        self.http.start()
        self.addCleanup(self.http.stop)
        self.sleep = mock.patch.object(WORKER.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.clock = mock.patch.object(
            WORKER.time, "monotonic", return_value=0
        )
        self.monotonic = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.lock = mock.patch.object(WORKER, "LOCK", threading.Lock())
        self.lock.start()
        self.addCleanup(self.lock.stop)

    def test_finished_query_accumulates_rows_without_cancelling(self):
        self.session.post.return_value = response(
            {"data": [[1]], "nextUri": NEXT_URI}
        )
        self.session.get.return_value = response({"data": [[2]]})
        self.assertEqual(WORKER.query("SELECT synthetic"), [[1], [2]])
        self.session.delete.assert_not_called()
        self.assertFalse(WORKER.STATE["blocked"])
        self.session.get.assert_called_once_with(
            NEXT_URI, timeout=30, allow_redirects=False
        )

    def test_terminal_query_error_needs_no_cancellation(self):
        self.session.post.return_value = response(
            {"error": {"errorName": "private-query-detail"}}
        )
        with self.assertRaises(RuntimeError) as error:
            WORKER.query("SELECT synthetic")
        self.assertNotIn("private-query-detail", str(error.exception))
        self.session.delete.assert_not_called()
        self.assertFalse(WORKER.STATE["blocked"])

    def test_deadline_cancels_the_latest_validated_result_uri(self):
        latest = NEXT_URI.rsplit("/", 1)[0] + "/2"
        self.session.get.return_value = response({"nextUri": latest})
        self.monotonic.side_effect = [0, 1, 181]
        with self.assertRaises(RuntimeError):
            WORKER.query("SELECT synthetic")
        self.session.delete.assert_called_once_with(
            latest, timeout=5, allow_redirects=False
        )
        self.assertFalse(WORKER.STATE["blocked"])

    def test_failed_poll_cancels_before_releasing_the_job_lock(self):
        self.session.get.side_effect = OSError("private-network-detail")
        self.session.delete.side_effect = lambda *args, **kwargs: (
            self.assertTrue(WORKER.LOCK.locked()) or response(status=204)
        )
        with (
            mock.patch.object(WORKER, "ingest", return_value={}),
            mock.patch.object(
                WORKER,
                "medallion",
                side_effect=lambda: WORKER.query("SELECT synthetic"),
            ),
        ):
            with self.assertRaises(RuntimeError) as error:
                WORKER.run("medallion")
        self.assertNotIn("private-network-detail", str(error.exception))
        self.assertFalse(WORKER.LOCK.locked())
        self.assertFalse(WORKER.STATE["blocked"])

    def test_cancel_404_confirms_that_the_query_is_gone(self):
        self.session.get.return_value = response(status=503)
        self.session.delete.return_value = response(status=404)
        with self.assertRaises(RuntimeError):
            WORKER.query("SELECT synthetic")
        self.assertFalse(WORKER.STATE["blocked"])

    def test_uncertain_cancel_blocks_every_subsequent_job(self):
        for result in [response(status=500), OSError("private-detail")]:
            with self.subTest(result=type(result).__name__):
                WORKER.STATE["blocked"] = False
                self.session.get.side_effect = OSError("poll failed")
                self.session.delete.side_effect = (
                    result if isinstance(result, Exception) else None
                )
                self.session.delete.return_value = result
                with self.assertRaises(RuntimeError):
                    WORKER.query("SELECT synthetic")
                self.assertTrue(WORKER.STATE["blocked"])
                with mock.patch.object(WORKER, "ingest") as importer:
                    with self.assertRaises(WORKER.WorkerBlockedError):
                        WORKER.run("ingest")
                    importer.assert_not_called()
                self.assertFalse(WORKER.LOCK.locked())

    def test_initial_post_failure_blocks_without_a_cancellation_uri(self):
        self.session.post.side_effect = OSError("private-initial-detail")
        with self.assertRaises(RuntimeError) as error:
            WORKER.query("SELECT synthetic")
        self.assertNotIn("private-initial-detail", str(error.exception))
        self.session.delete.assert_not_called()
        self.assertTrue(WORKER.STATE["blocked"])

    def test_untrusted_uri_is_never_fetched_or_cancelled(self):
        for uri in [
            "http://outside.invalid:8080/statement/1",
            "http://trino:9999/statement/1",
            "http://user:password@trino:8080/statement/1",
            "https://trino:8080/statement/1",
        ]:
            with self.subTest(uri=uri):
                WORKER.STATE["blocked"] = False
                self.session.post.return_value = response({"nextUri": uri})
                with self.assertRaises(RuntimeError):
                    WORKER.query("SELECT synthetic")
                self.session.get.assert_not_called()
                self.session.delete.assert_not_called()
                self.assertTrue(WORKER.STATE["blocked"])

    def test_blocked_worker_returns_a_fixed_http_409(self):
        WORKER.STATE["blocked"] = True
        payload = json.dumps({"action": "ingest"}).encode()
        handler = object.__new__(WORKER.Handler)
        handler.path = "/run"
        handler.headers = {
            "Authorization": "Bearer synthetic",
            "Content-Length": str(len(payload)),
        }
        handler.rfile = io.BytesIO(payload)
        handler.respond = mock.Mock()
        with mock.patch.dict(os.environ, {"WORKER_TOKEN": "synthetic"}):
            handler.do_POST()
        status, result = handler.respond.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(result["error"], "WorkerBlockedError")
        self.assertIn("restart the worker", result["message"])

    def test_uncertain_query_leaves_verification_data_for_review(self):
        def uncertain_import(*_):
            WORKER.STATE["blocked"] = True
            raise RuntimeError("Trino query failed.")

        with (
            mock.patch.object(WORKER, "catalog"),
            mock.patch.object(WORKER.subprocess, "run"),
            mock.patch.object(WORKER, "ingest", side_effect=uncertain_import),
            mock.patch.object(WORKER, "cleanup_verification") as cleanup,
        ):
            with self.assertRaises(RuntimeError):
                WORKER.verify()
            cleanup.assert_not_called()

    def test_known_terminal_failure_still_cleans_verification_data(self):
        with (
            mock.patch.object(WORKER, "catalog"),
            mock.patch.object(WORKER.subprocess, "run"),
            mock.patch.object(
                WORKER, "ingest", side_effect=ValueError("fixture invalid")
            ),
            mock.patch.object(WORKER, "cleanup_verification") as cleanup,
        ):
            with self.assertRaises(ValueError):
                WORKER.verify()
            cleanup.assert_called_once()
            self.assertRegex(cleanup.call_args.args[1], r"^verify_[0-9a-f]+_$")


class CompletionMarkerTests(unittest.TestCase):
    """Validate marker format and read limits, including a concurrent grow."""

    def test_lf_and_crlf_markers_are_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "source.csv.ready"
            for newline in [b"\n", b"\r\n"]:
                marker.write_bytes(b"a" * 64 + newline)
                self.assertEqual(WORKER.completion_hash(marker), "a" * 64)

    def test_extra_whitespace_and_missing_newlines_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "source.csv.ready"
            for content in [b"a" * 64, b"a" * 64 + b" \n", b"A" * 64 + b"\n"]:
                with self.subTest(content_length=len(content)):
                    marker.write_bytes(content)
                    with self.assertRaises(ValueError):
                        WORKER.completion_hash(marker)

    def test_huge_marker_is_rejected_before_opening_its_contents(self):
        marker = mock.Mock(spec=Path)
        marker.stat.return_value.st_size = 64 * 1024 * 1024
        with self.assertRaises(ValueError):
            WORKER.completion_hash(marker)
        marker.open.assert_not_called()

    def test_marker_that_grew_after_stat_is_read_at_most_67_bytes(self):
        marker = mock.Mock(spec=Path)
        marker.stat.return_value.st_size = 65
        stream = mock.Mock(wraps=io.BytesIO(b"a" * 1024 * 1024))
        marker.open.return_value.__enter__ = mock.Mock(return_value=stream)
        marker.open.return_value.__exit__ = mock.Mock(return_value=False)
        with self.assertRaises(ValueError):
            WORKER.completion_hash(marker)
        stream.read.assert_called_once_with(67)


if __name__ == "__main__":
    unittest.main()
