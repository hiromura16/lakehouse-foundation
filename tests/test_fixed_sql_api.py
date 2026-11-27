"""Mocked API contracts for fixed SQL controls; no SQL Server is contacted."""

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from hashlib import sha256
from pathlib import Path
from unittest import mock

# Skip only an absent optional package; keep broken installs as errors.
try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError as error:
    if (
        error.name != "fastapi"
        or importlib.util.find_spec("fastapi") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional tests need fastapi. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
with mock.patch.object(sys, "path", [str(ROOT / "infra/demo"), *sys.path]):
    SPEC = importlib.util.spec_from_file_location(
        "fixed_sql_api_subject", ROOT / "infra/demo/api.py"
    )
    API = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(API)
SQL_API = sys.modules["sqlsource_api"]


class FixedSqlApiTests(unittest.TestCase):
    """Exercise authentication, strict consent, safe status and job routing."""

    def setUp(self):
        """Build a real ASGI app using only generated synthetic settings."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        path = self.root / "private/site.env"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(path),
            ],
            capture_output=True,
            check=True,
        )
        self.app = API.create_app(
            path,
            token="fixed-sql-unit-api",
            landing=self.root / "landing",
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer fixed-sql-unit-api"}
        self.adapter = mock.Mock()
        patch = mock.patch.object(
            SQL_API, "source_diagnostic_module", return_value=self.adapter
        )
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, method, path, **kwargs):
        """Use real routing and validation without real network/DB traffic."""
        return self.client.request(
            method, path, headers=self.headers, **kwargs
        )

    def wait(self, identifier):
        """Observe the finite job thread through its terminal status."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            response = self.request("GET", "/jobs/" + identifier).json()
            if response["status"] in {"succeeded", "failed"}:
                return response
            time.sleep(0.01)
        self.fail("Synthetic job did not finish.")

    def test_diagnostics_filter_private_details_and_share_operation_lock(self):
        self.adapter.diagnose.return_value = {
            "connected": True,
            "ownership": True,
            "permissions_safe": True,
            "schema_version": 1,
            "tables": {},
            "password": "synthetic-private-do-not-return",
        }
        self.assertEqual(
            self.client.get("/sql-source/status").status_code, 401
        )
        self.app.state.jobs.operation.acquire()
        try:
            self.assertEqual(
                self.request("GET", "/sql-source/status").status_code, 409
            )
            self.adapter.diagnose.assert_not_called()
        finally:
            self.app.state.jobs.operation.release()
        reply = self.request("GET", "/sql-source/status")
        self.assertEqual(reply.status_code, 200)
        self.assertNotIn("synthetic-private", reply.text)
        settings = self.adapter.diagnose.call_args.args[0]
        self.assertEqual(
            self.adapter.diagnose.call_args.kwargs, {"role": "source"}
        )
        self.assertIn("SOURCE_SQLSERVER_PASSWORD", settings)
        self.assertNotIn("SOURCE_SQLSERVER_CONTROL_PASSWORD", settings)
        self.assertNotIn("DEMO_SQLSERVER_PASSWORD", settings)

    def test_checkpoint_diagnostic_is_authenticated_and_filters_secrets(self):
        """Use the control-only observer without enqueuing a write job."""
        implementation = mock.Mock()
        implementation.inspect_checkpoints.return_value = {
            "configured": True,
            "baseline_complete": False,
            "watermarks": {},
            "password": "synthetic-private-do-not-return",
        }
        with mock.patch.object(
            SQL_API, "batch_module", return_value=implementation
        ):
            self.assertEqual(
                self.client.get("/sql-source/checkpoints").status_code, 401
            )
            reply = self.request("GET", "/sql-source/checkpoints")
        self.assertEqual(reply.status_code, 200)
        self.assertFalse(reply.json()["baseline_complete"])
        self.assertNotIn("synthetic-private", reply.text)
        selected = implementation.inspect_checkpoints.call_args.args[0]
        self.assertNotIn("DEMO_SQLSERVER_PASSWORD", selected)
        self.assertIn("SOURCE_SQLSERVER_CONTROL_PASSWORD", selected)

    def test_checkpoint_diagnostic_shares_operation_lock_and_redacts_errors(
        self,
    ):
        """Never observe during a checkpoint transaction or leak a driver."""
        implementation = mock.Mock()
        with mock.patch.object(
            SQL_API, "batch_module", return_value=implementation
        ):
            self.app.state.jobs.operation.acquire()
            try:
                reply = self.request("GET", "/sql-source/checkpoints")
                self.assertEqual(reply.status_code, 409)
                implementation.inspect_checkpoints.assert_not_called()
            finally:
                self.app.state.jobs.operation.release()
            implementation.inspect_checkpoints.side_effect = RuntimeError(
                "synthetic-private-driver-error"
            )
            reply = self.request("GET", "/sql-source/checkpoints")
        self.assertEqual(reply.status_code, 422)
        self.assertNotIn("synthetic-private", reply.text)
        self.assertFalse(self.app.state.jobs.operation.locked())

    def test_fixed_dml_routes_are_absent(self):
        """The old CRUD URLs must not dispatch or appear in OpenAPI."""
        for action in ("insert", "update", "delete"):
            reply = self.request(
                "POST",
                "/sql-source/demo/" + action,
                json={"confirm": True, "run_id": "012345abcdef"},
            )
            self.assertEqual(reply.status_code, 404)
        self.assertNotIn(
            "/sql-source/demo/{action}", self.app.openapi()["paths"]
        )
        self.adapter.mutate.assert_not_called()

    def test_driver_failure_is_redacted_from_read_status(self):
        self.adapter.diagnose.side_effect = ValueError("synthetic-private-url")
        reply = self.request("GET", "/sql-source/status")
        self.assertEqual(reply.status_code, 422)
        self.assertNotIn("synthetic-private", reply.text)
        self.assertFalse(self.app.state.jobs.operation.locked())

    def test_snapshot_separates_reader_and_checkpoint_auth_from_worker(self):
        """Keep both principals source-side and exclude DEMO write auth."""
        endpoint = "/sql-source/batches/snapshot"
        for body in (
            {"confirm": False},
            {"confirm": 1},
            {"confirm": True, "table": "dbo.private"},
        ):
            self.assertEqual(
                self.request("POST", endpoint, json=body).status_code, 422
            )
        selected = self.app.state.store.applied
        selected.update(
            SOURCE_SQLSERVER_PASSWORD="synthetic-reader-private",
            SOURCE_SQLSERVER_CONTROL_PASSWORD="synthetic-control-private",
            DEMO_SQLSERVER_PASSWORD="synthetic-demo-private",
        )
        module = mock.Mock()
        batch_id = "d" * 64
        observed_at = "2026-01-01T00:00:00.1234567Z"
        module.run_snapshot.return_value = {
            "completed": True,
            "batch_id": batch_id,
            "apply_id": sha256(
                (batch_id + "\n" + observed_at).encode()
            ).hexdigest(),
            "observed_at": observed_at,
            "skipped": False,
            "sql_statements": 14,
            "counts": dict.fromkeys(
                SQL_API.send_batch.__globals__["CONTRACT_IDS"], 1
            ),
            "password": "synthetic-private-detail",
        }
        with mock.patch.object(SQL_API, "batch_module", return_value=module):
            reply = self.request("POST", endpoint, json={"confirm": True})
            result = self.wait(reply.json()["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("synthetic-private", str(result))
        arguments = module.run_snapshot.call_args.args
        reader = arguments[0]
        self.assertEqual(
            reader["SOURCE_SQLSERVER_PASSWORD"], "synthetic-reader-private"
        )
        self.assertNotIn("DEMO_SQLSERVER_PASSWORD", reader)
        self.assertEqual(
            reader["SOURCE_SQLSERVER_CONTROL_PASSWORD"],
            "synthetic-control-private",
        )
        self.assertNotIn("DEMO_SQLSERVER_USER", reader)
        self.assertEqual(arguments[1], self.app.state.jobs.output)

    def test_snapshot_failure_redacts_driver_and_releases_job(self):
        module = mock.Mock()
        module.run_snapshot.side_effect = ValueError("synthetic-private-url")
        with mock.patch.object(SQL_API, "batch_module", return_value=module):
            reply = self.request(
                "POST", "/sql-source/batches/snapshot", json={"confirm": True}
            )
            result = self.wait(reply.json()["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("synthetic-private", str(result))
        self.assertFalse(self.app.state.jobs.operation.locked())

    def test_incremental_requires_confirmed_checkpoint_and_filters_metadata(
        self,
    ):
        """Require the source-side checkpoint transaction to succeed."""
        batch_id = "e" * 64
        stamp = "2026-01-01T00:00:00.1234567Z"
        response = {
            "batch_id": batch_id,
            "observed_at": stamp,
            "apply_id": sha256((batch_id + "\n" + stamp).encode()).hexdigest(),
            "completed": True,
            "skipped": False,
            "sql_statements": 14,
            "counts": dict.fromkeys(
                SQL_API.send_batch.__globals__["CONTRACT_IDS"], 1
            ),
            "checkpointed": False,
            "password": "synthetic-private-detail",
        }
        module = mock.Mock()
        with mock.patch.object(SQL_API, "batch_module", return_value=module):
            for checkpointed in (False, True):
                module.run_incremental.return_value = {
                    **response,
                    "checkpointed": checkpointed,
                }
                reply = self.request(
                    "POST",
                    "/sql-source/batches/incremental",
                    json={"confirm": True},
                )
                result = self.wait(reply.json()["id"])
                self.assertEqual(
                    result["status"], "succeeded" if checkpointed else "failed"
                )
                self.assertNotIn("synthetic-private", str(result))

    def test_unknown_batch_mode_and_custom_watermark_are_rejected(self):
        """Users select a fixed mode, never source tables, SQL or cursors."""
        self.assertEqual(
            self.request(
                "POST", "/sql-source/batches/reset", json={"confirm": True}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.request(
                "POST",
                "/sql-source/batches/incremental",
                json={
                    "confirm": True,
                    "watermark": "synthetic-private-cursor",
                },
            ).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
