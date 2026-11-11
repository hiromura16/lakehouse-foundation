"""Mocked API contracts for fixed SQL controls; no SQL Server is contacted."""

import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

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
        self.generator = mock.Mock(return_value={"sources": []})
        self.app = API.create_app(
            path,
            token="fixed-sql-unit-api",
            generator=self.generator,
            landing=self.root / "landing",
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer fixed-sql-unit-api"}
        self.adapter = mock.Mock()
        patch = mock.patch.object(
            SQL_API, "fixed_sql_module", return_value=self.adapter
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

    def test_fixed_generation_requires_two_consents_and_rejects_unknown_sql(
        self,
    ):
        base = {"destination": "fixed-sqlserver", "allow_sql_write": True}
        for extra in (
            {},
            {"confirm_shared_source_database": 1},
            {"confirm_shared_source_database": True, "schema": "dbo"},
        ):
            with self.subTest(extra=extra):
                reply = self.request("POST", "/jobs", json={**base, **extra})
                self.assertEqual(reply.status_code, 422)
        self.generator.assert_not_called()

    def test_run_id_and_fixed_consent_reach_source_only_generator(self):
        run_id = "012345abcdef"
        reply = self.request(
            "POST",
            "/jobs",
            json={
                "destination": "fixed-sqlserver",
                "allow_sql_write": True,
                "confirm_shared_source_database": True,
                "run_id": run_id,
                "scenario": "clean",
            },
        )
        self.assertEqual(reply.status_code, 202)
        result = self.wait(reply.json()["id"])
        self.assertEqual(result["status"], "succeeded")
        arguments = self.generator.call_args.kwargs
        self.assertEqual(arguments["run_id"], run_id)
        self.assertTrue(arguments["confirm_shared_source_database"])
        self.assertEqual(arguments["destination"], "fixed-sqlserver")

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

    def test_dml_is_fixed_authenticated_and_requires_strict_boolean(self):
        endpoint = "/sql-source/demo/insert"
        for body in (
            {"confirm": 1, "run_id": "012345abcdef"},
            {"confirm": False, "run_id": "012345abcdef"},
            {
                "confirm": True,
                "run_id": "012345abcdef",
                "sql": "synthetic-private-sql",
            },
        ):
            reply = self.request("POST", endpoint, json=body)
            self.assertEqual(reply.status_code, 422)
            self.assertNotIn("synthetic-private", reply.text)
        self.adapter.mutate.assert_not_called()
        self.assertEqual(
            self.request(
                "POST",
                "/sql-source/demo/drop",
                json={
                    "confirm": True,
                    "run_id": "012345abcdef",
                },
            ).status_code,
            422,
        )
        self.adapter.mutate.return_value = {
            "changed_rows": 1,
            "password": "synthetic-private-detail",
        }
        reply = self.request(
            "POST",
            endpoint,
            json={
                "confirm": True,
                "run_id": "012345abcdef",
            },
        )
        self.assertEqual(reply.status_code, 202)
        result = self.wait(reply.json()["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("synthetic-private", str(result))
        self.assertTrue(self.adapter.mutate.call_args.kwargs["consent"])

    def test_driver_failure_is_redacted_from_status_and_job(self):
        self.adapter.diagnose.side_effect = ValueError("synthetic-private-url")
        reply = self.request("GET", "/sql-source/status")
        self.assertEqual(reply.status_code, 422)
        self.assertNotIn("synthetic-private", reply.text)
        self.adapter.mutate.side_effect = ValueError("synthetic-private-url")
        reply = self.request(
            "POST",
            "/sql-source/demo/delete",
            json={
                "confirm": True,
                "run_id": "012345abcdef",
            },
        )
        result = self.wait(reply.json()["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("synthetic-private", str(result))
        self.assertFalse(self.app.state.jobs.operation.locked())


if __name__ == "__main__":
    unittest.main()
