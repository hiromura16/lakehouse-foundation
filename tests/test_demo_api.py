"""Check settings, external-input processing, and fixed API boundaries."""

import contextlib
import importlib.util
import io
import json
import runpy
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

# Only a genuinely absent optional package skips this test module. A broken
# installed package or any application import failure must remain an error.
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
DEMO = ROOT / "infra/demo"
with mock.patch.object(sys, "path", [str(DEMO), *sys.path]):
    SPEC = importlib.util.spec_from_file_location(
        "demo_api_subject", DEMO / "api.py"
    )
    API = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(API)


class ControlApiTests(unittest.TestCase):
    """Use real FastAPI routing and validation without external services."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="demo-api-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.private = self.directory / "private/site.env"
        validator = sys.modules["control_settings"].VALIDATOR
        with (
            mock.patch.dict(sys.modules, {"settings": validator}),
            mock.patch.object(
                sys,
                "argv",
                ["configure.py", "--output", str(self.private)],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(
                str(ROOT / "scripts/configure.py"), run_name="__main__"
            )
        self.ai_exporter = mock.Mock(return_value={"datasets": []})
        self.app = API.create_app(
            self.private,
            token="unit-api-token",
            landing=self.directory / "landing",
            ai_exporter=self.ai_exporter,
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer unit-api-token"}

    def request(self, method, path, **kwargs):
        return self.client.request(
            method, path, headers=self.headers, **kwargs
        )

    def wait_job(self, identifier):
        """Wait briefly for a local thread without contacting real services."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = self.request("GET", "/jobs/" + identifier).json()
            if value["status"] in {"succeeded", "failed", "cancelled"}:
                return value
            time.sleep(0.005)
        self.fail("The local test job did not finish.")

    def test_removed_writer_routes_and_preserved_processing(self):
        """投入スキーマを廃止し後続処理の照会と受付を残す。"""
        paths = self.client.get("/openapi.json").json()["paths"]
        for path in (
            "/scenarios",
            "/cdc/demo/{action}",
            "/jobs/{identifier}/cancel",
        ):
            self.assertNotIn(path, paths)
        self.assertEqual(set(paths["/jobs"]), {"get"})
        self.assertEqual(
            self.request("POST", "/jobs", json={}).status_code, 405
        )
        self.assertEqual(
            self.request("POST", "/cdc/demo/insert").status_code, 404
        )
        self.assertEqual(self.request("GET", "/jobs").json()["jobs"], [])
        self.assertIn("/pipeline/{action}", paths)
        self.assertIn("/sql-source/batches/{mode}", paths)

    def test_swagger_assets_do_not_require_a_browser_cdn(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/swagger-ui-bundle.js", response.text)
        self.assertIn("/static/swagger-ui.css", response.text)
        self.assertNotIn("cdn.jsdelivr.net", response.text)
        self.assertNotIn("https://", response.text)
        self.assertIn('"validatorUrl": null', response.text)
        self.assertIn('"persistAuthorization": false', response.text)

    def test_auth_is_required_and_swagger_describes_bearer(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/settings").status_code, 401)
        response = self.client.get(
            "/settings", headers={"Authorization": "Bearer incorrect"}
        )
        self.assertEqual(response.json()["code"], "AUTH_REQUIRED")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertIn("JobView", schema["components"]["schemas"])
        self.assertNotIn("SourceJob", schema["components"]["schemas"])

    def test_settings_never_return_secrets_or_authentication_values(self):
        values = self.app.state.store.read()
        response = self.request("GET", "/settings")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn("S3_SECRET_KEY", result["configured_secrets"])
        self.assertNotIn("S3_SECRET_KEY", result["values"])
        for key in result["configured_secrets"]:
            self.assertNotIn(values[key], response.text)
        self.assertNotIn("unit-api-token", response.text)

    def test_blank_secret_is_kept_and_settings_are_saved_not_applied(self):
        original = self.app.state.store.read()["S3_SECRET_KEY"]
        response = self.request(
            "PATCH",
            "/settings",
            json={"values": {"S3_SECRET_KEY": "", "TRINO_PORT": "8090"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.app.state.store.read()["S3_SECRET_KEY"], original
        )
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)
        self.assertTrue(response.json()["saved"])
        self.assertFalse(response.json()["applied"])
        self.assertTrue(response.json()["pending_restart"])
        self.assertEqual(self.app.state.store.applied["TRINO_PORT"], "8080")

    def test_invalid_settings_are_rejected_without_replacing_the_file(self):
        original = self.private.read_bytes()
        for updates in [
            {"DOCKER_HOST": "http://private.invalid"},
            {"TRINO_PORT": "not-a-port"},
            {"SOURCE_SQLSERVER_PASSWORD": "sentinel\nprivate-value"},
            {"CONTROL_SETTINGS_DIR": "/srv/another-private-directory"},
        ]:
            with self.subTest(keys=list(updates)):
                response = self.request(
                    "PATCH", "/settings", json={"values": updates}
                )
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("sentinel", response.text)
                self.assertEqual(self.private.read_bytes(), original)

    def test_cdc_secrets_are_hidden_and_mount_save_requires_redeployment(self):
        response = self.request(
            "PATCH",
            "/settings",
            json={
                "values": {
                    "CDC_CONFIG_DIR": "/srv/lakehouse/cdc-private",
                    "CDC_SQLSERVER_PASSWORD": "unit-capture-secret",
                    "CDC_SQLSERVER_TRUSTSTORE_PASSWORD": "unit-trust-secret",
                    "CDC_TOPIC_PREFIX": "factory",
                }
            },
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["pending_mount_change"])
        self.assertIn("CDC_SQLSERVER_PASSWORD", result["configured_secrets"])
        self.assertIn(
            "CDC_SQLSERVER_TRUSTSTORE_PASSWORD", result["configured_secrets"]
        )
        self.assertEqual(result["values"]["CDC_TOPIC_PREFIX"], "factory")
        self.assertNotIn("unit-capture-secret", response.text)
        self.assertNotIn("unit-trust-secret", response.text)

    def test_landing_save_does_not_retarget_the_active_mount(self):
        current_output = self.app.state.jobs.output
        response = self.request(
            "PATCH",
            "/settings",
            json={"values": {"LANDING_DIR": "/srv/lakehouse/alternate"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["pending_mount_change"])
        self.assertEqual(self.app.state.jobs.output, current_output)

    def test_pipeline_returns_job_id_without_waiting_and_filters_worker_data(
        self,
    ):
        release = threading.Event()
        started = threading.Event()
        self.addCleanup(release.set)
        driver = mock.Mock()
        driver.__enter__ = mock.Mock(return_value=driver)
        driver.__exit__ = mock.Mock(return_value=False)

        def delayed_worker(*_, **__):
            started.set()
            release.wait(2)
            response = mock.Mock(status_code=200)
            response.json.return_value = {
                "ok": True,
                "imported": 6,
                "sql_statements": 14,
                "secret": "sentinel-private-worker-detail",
            }
            return response

        driver.post.side_effect = delayed_worker
        driver.get.return_value.status_code = 200
        # Legacy health documents omit busy and remain compatible.
        driver.get.return_value.json.return_value = {"ready": True}
        with mock.patch.object(API.httpx, "Client", return_value=driver):
            accepted = self.request("POST", "/pipeline/medallion")
            self.assertEqual(accepted.status_code, 202)
            identifier = accepted.json()["id"]
            self.assertTrue(started.wait(1))
            self.assertEqual(
                self.request("GET", "/jobs/" + identifier).json()["status"],
                "running",
            )
            self.assertEqual(
                self.request(
                    "POST", "/jobs/" + identifier + "/cancel"
                ).status_code,
                404,
            )
            release.set()
            result = self.wait_job(identifier)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("sentinel-private-worker-detail", json.dumps(result))
        self.assertEqual(
            driver.post.call_args.args[0], "http://worker:8000/run"
        )

    def test_pipeline_refuses_busy_or_blocked_worker_without_queuing(self):
        driver = mock.MagicMock()
        driver.__enter__.return_value = driver
        for payload, status in (
            ({"ready": True, "busy": True}, 200),
            ({"ready": False, "blocked": True}, 503),
        ):
            with self.subTest(payload=payload):
                driver.get.return_value.status_code = status
                driver.get.return_value.json.return_value = payload
                with mock.patch.object(
                    API.httpx, "Client", return_value=driver
                ):
                    result = self.request("POST", "/pipeline/medallion")
                self.assertEqual(result.status_code, 409)
                self.assertEqual(result.json()["code"], "WORKER_NOT_IDLE")
                self.assertEqual(self.app.state.jobs.jobs, {})
                driver.post.assert_not_called()
        driver.get.assert_called_with("http://worker:8000/health")

    def test_pipeline_timeout_marks_worker_result_unknown_without_retry(self):
        driver = mock.MagicMock()
        driver.__enter__.return_value = driver
        driver.get.return_value.status_code = 200
        driver.get.return_value.json.return_value = {"busy": False}
        driver.post.side_effect = API.httpx.ReadTimeout(
            "unit-private-worker-uri-and-password"
        )
        with mock.patch.object(API.httpx, "Client", return_value=driver):
            accepted = self.request("POST", "/pipeline/medallion")
            self.assertEqual(accepted.status_code, 202)
            result = self.wait_job(accepted.json()["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "WORKER_RESULT_UNKNOWN")
        self.assertIn("継続中", result["error"]["message"])
        self.assertNotIn("unit-private-worker", json.dumps(result))
        driver.post.assert_called_once()
        self.assertFalse(self.app.state.jobs.operation.locked())

    def test_pipeline_health_failure_returns_503_without_queuing(self):
        driver = mock.MagicMock()
        driver.__enter__.return_value = driver
        driver.get.side_effect = API.httpx.ConnectError(
            "unit-private-health-endpoint"
        )
        with mock.patch.object(API.httpx, "Client", return_value=driver):
            result = self.request("POST", "/pipeline/verify")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["code"], "WORKER_UNAVAILABLE")
        self.assertNotIn("unit-private-health", result.text)
        self.assertEqual(self.app.state.jobs.jobs, {})
        driver.post.assert_not_called()

    def test_status_uses_only_known_internal_urls(self):
        driver = mock.Mock()
        driver.__enter__ = mock.Mock(return_value=driver)
        driver.__exit__ = mock.Mock(return_value=False)
        driver.get.return_value.status_code = 200
        driver.get.return_value.json.return_value = {
            "busy": True,
            "private": "unit-private-health-detail",
        }
        with mock.patch.object(
            API.httpx, "Client", return_value=driver
        ) as client:
            result = self.request("GET", "/status?url=http://outside.invalid")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(
            {call.args[0] for call in driver.get.call_args_list},
            set(API.KNOWN_HEALTH_URLS.values()),
        )
        self.assertFalse(client.call_args.kwargs["trust_env"])
        self.assertFalse(client.call_args.kwargs["follow_redirects"])
        self.assertTrue(result.json()["services"]["worker"]["busy"])
        self.assertNotIn("unit-private-health-detail", result.text)
        driver.get.return_value.json.return_value = {"busy": "true"}
        with mock.patch.object(API.httpx, "Client", return_value=driver):
            result = self.request("GET", "/status")
        self.assertNotIn("busy", result.json()["services"]["worker"])

    def test_ai_route_is_bounded_and_preserves_decimal_facts(self):
        self.ai_exporter.return_value = {
            "amount": Decimal("12.50"),
            "at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        response = self.request("GET", "/ai/context?dataset=sales&limit=20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["amount"], "12.50")
        self.ai_exporter.assert_called_once_with(["sales"], 20)
        self.assertEqual(
            self.request("GET", "/ai/context?limit=501").status_code, 422
        )
        self.ai_exporter.side_effect = RuntimeError("sentinel-private-query")
        failed = self.request("GET", "/ai/context")
        self.assertEqual(failed.status_code, 409)
        self.assertEqual(failed.json()["code"], "GOLD_UNAVAILABLE")
        self.assertNotIn("sentinel-private-query", failed.text)


if __name__ == "__main__":
    unittest.main()
