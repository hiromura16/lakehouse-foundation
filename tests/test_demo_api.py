"""Check authenticated settings, source-only jobs, and fixed API boundaries."""

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

from fastapi.testclient import TestClient

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
        self.generator = mock.Mock(
            side_effect=lambda _, **options: {
                "run_id": options["run_id"],
                "batch": options["batch"],
                "sources": [],
                "manifest": "manifests/synthetic.json",
            }
        )
        self.ai_exporter = mock.Mock(return_value={"datasets": []})
        self.cdc_runner = mock.Mock(
            side_effect=lambda _, action: {
                "action": action,
                "changed_rows": 6,
                "source_only": True,
                "private_detail": "unit-private-helper-detail",
            }
        )
        self.app = API.create_app(
            self.private,
            token="unit-api-token",
            landing=self.directory / "landing",
            generator=self.generator,
            ai_exporter=self.ai_exporter,
            cdc_runner=self.cdc_runner,
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

    def prepare_cdc(self):
        """Configure synthetic ownership without touching a real database."""
        store = self.app.state.store
        store.save(
            {
                "DEMO_SQLSERVER_HOST": "demo-db.example.invalid",
                "DEMO_SQLSERVER_USER": "unit-demo-admin",
                "DEMO_SQLSERVER_PASSWORD": "unit-demo-password",
                "CDC_SQLSERVER_DATABASE": "lh_cdc_demo_0123456789abcdef",
                "CDC_DEMO_OWNERSHIP_TOKEN": "a" * 64,
            }
        )
        store.applied = store.read()

    def test_swagger_assets_do_not_require_a_browser_cdn(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/static/swagger-ui-bundle.js", response.text)
        self.assertIn("/static/swagger-ui.css", response.text)
        self.assertNotIn("cdn.jsdelivr.net", response.text)
        self.assertNotIn("https://", response.text)
        self.assertIn('"validatorUrl": null', response.text)
        self.assertIn('"persistAuthorization": false', response.text)

    def test_cdc_dml_requires_auth_confirmation_and_prepared_ownership(self):
        self.assertEqual(
            self.client.post(
                "/cdc/demo/insert", json={"confirm": True}
            ).status_code,
            401,
        )
        for payload in (
            {},
            {"confirm": False},
            {"confirm": 1},
            {"confirm": "true"},
            {"confirm": True, "trust_local_test": True},
            {"confirm": True, "sql": "private-sentinel"},
        ):
            with self.subTest(payload=payload):
                response = self.request(
                    "POST", "/cdc/demo/insert", json=payload
                )
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("private-sentinel", response.text)
        response = self.request(
            "POST", "/cdc/demo/insert", json={"confirm": True}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "CDC_DEMO_NOT_PREPARED")
        self.prepare_cdc()
        self.app.state.store.applied["SOURCE_SQLSERVER_DATABASE"] = (
            "lh_cdc_demo_0123456789abcdef"
        )
        self.assertEqual(
            self.request(
                "POST", "/cdc/demo/insert", json={"confirm": True}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.request(
                "POST", "/cdc/demo/drop", json={"confirm": True}
            ).status_code,
            422,
        )
        self.cdc_runner.assert_not_called()

    def test_cdc_dml_is_source_only_and_returns_filtered_job_metadata(self):
        self.prepare_cdc()
        with mock.patch.object(API.httpx, "Client") as outbound:
            for action in ("insert", "update", "delete"):
                accepted = self.request(
                    "POST", "/cdc/demo/" + action, json={"confirm": True}
                )
                self.assertEqual(accepted.status_code, 202)
                self.assertEqual(accepted.json()["kind"], "cdc-source")
                result = self.wait_job(accepted.json()["id"])
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["cdc_action"], action)
                self.assertEqual(result["completed"], 1)
                self.assertEqual(
                    result["results"],
                    [
                        {
                            "action": action,
                            "changed_rows": 6,
                            "source_only": True,
                        }
                    ],
                )
                self.assertNotIn("unit-private-helper-detail", str(result))
            outbound.assert_not_called()
        self.generator.assert_not_called()
        values = self.cdc_runner.call_args.args[0]
        self.assertIn("CDC_DEMO_OWNERSHIP_TOKEN", values)
        self.assertIn("SOURCE_SQLSERVER_DATABASE", values)
        self.assertNotIn("WORKER_TOKEN", values)
        self.assertNotIn("S3_SECRET_KEY", values)
        self.assertNotIn("CDC_TEST_TRUST_SERVER_CERTIFICATE", values)
        public = self.request("GET", "/settings")
        self.assertNotIn('"' + "a" * 64 + '"', public.text)
        self.assertIn(
            "CDC_DEMO_OWNERSHIP_TOKEN", public.json()["configured_secrets"]
        )

    def test_cdc_dml_serializes_with_other_operations_and_cannot_cut_sql(self):
        self.prepare_cdc()
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def delayed_source(_, action):
            started.set()
            release.wait(2)
            return {"action": action, "changed_rows": 6, "source_only": True}

        self.cdc_runner.side_effect = delayed_source
        accepted = self.request(
            "POST", "/cdc/demo/insert", json={"confirm": True}
        ).json()
        self.assertTrue(started.wait(1))
        for path, payload in (
            ("/cdc/demo/update", {"confirm": True}),
            ("/jobs", {}),
            ("/pipeline/medallion", None),
        ):
            self.assertEqual(
                self.request("POST", path, json=payload).status_code, 409
            )
        self.assertEqual(
            self.request(
                "PATCH", "/settings", json={"values": {}}
            ).status_code,
            409,
        )
        cancelled = self.request("POST", "/jobs/" + accepted["id"] + "/cancel")
        self.assertEqual(cancelled.status_code, 409)
        self.assertEqual(cancelled.json()["code"], "CDC_DEMO_RUNNING")
        release.set()
        self.assertEqual(self.wait_job(accepted["id"])["status"], "succeeded")

    def test_cdc_dml_failures_do_not_return_driver_details(self):
        self.prepare_cdc()
        self.cdc_runner.side_effect = ValueError("unit-private-db-password")
        accepted = self.request(
            "POST", "/cdc/demo/update", json={"confirm": True}
        ).json()
        result = self.wait_job(accepted["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "CDC_DEMO_FAILED")
        self.assertNotIn("unit-private-db-password", json.dumps(result))

    def test_cdc_bridge_forces_normal_tls(self):
        module = mock.Mock()
        specification = mock.Mock()
        with (
            mock.patch.object(
                API.importlib.util,
                "spec_from_file_location",
                return_value=specification,
            ),
            mock.patch.object(
                API.importlib.util, "module_from_spec", return_value=module
            ),
        ):
            API.apply_cdc_demo({"synthetic": "settings"}, "insert")
        module.apply_scenario.assert_called_once_with(
            {"synthetic": "settings"}, "insert", trust_local_test=False
        )

    def test_auth_is_required_and_swagger_describes_bearer(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/settings").status_code, 401)
        response = self.client.get(
            "/settings", headers={"Authorization": "Bearer incorrect"}
        )
        self.assertEqual(response.json()["code"], "AUTH_REQUIRED")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("HTTPBearer", schema["components"]["securitySchemes"])
        self.assertIn("SourceJob", schema["components"]["schemas"])

    def test_scenario_catalog_is_authenticated_and_matches_shared_metadata(
        self,
    ):
        self.assertEqual(self.client.get("/scenarios").status_code, 401)
        with mock.patch.object(API.httpx, "Client") as outbound:
            response = self.request("GET", "/scenarios")
            outbound.assert_not_called()
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        engine = API.source_scenario_engine()
        self.assertEqual(catalog["version"], 1)
        self.assertEqual(catalog["scenarios"], engine.catalog())
        self.assertEqual(catalog["presets"], engine.presets())
        self.assertEqual(
            catalog["conflicts"], [sorted(group) for group in engine.CONFLICTS]
        )
        schema = self.client.get("/openapi.json").json()
        self.assertIn("ScenarioCatalog", schema["components"]["schemas"])
        self.assertIn(
            "anomalies",
            schema["components"]["schemas"]["SourceJob"]["properties"],
        )

    def test_selected_anomalies_and_rate_reach_source_only_generator(self):
        with mock.patch.object(API.httpx, "Client") as outbound:
            accepted = self.request(
                "POST",
                "/jobs",
                json={
                    "scenario": "clean",
                    "anomalies": ["missing_required", "duplicate_row"],
                    "anomaly_rate": 35,
                },
            )
            self.assertEqual(accepted.status_code, 202)
            result = self.wait_job(accepted.json()["id"])
            outbound.assert_not_called()
        self.assertEqual(result["status"], "succeeded")
        options = self.generator.call_args.kwargs
        self.assertEqual(
            options["anomalies"], ["duplicate_row", "missing_required"]
        )
        self.assertEqual(options["anomaly_rate"], 35)
        self.assertFalse(options["allow_file_faults"])
        self.assertEqual(options["scenario"], "clean")
        self.assertNotIn("WORKER_TOKEN", options["settings"])

    def test_invalid_or_incompatible_anomalies_never_start_source_generation(
        self,
    ):
        requests = [
            {"anomalies": ["missing_required", "missing_required"]},
            {"anomalies": ["private-sentinel-unknown"]},
            {"anomalies": "missing_required"},
            {"anomalies": ["missing_required"] * 33},
            {"anomaly_rate": 0},
            {"anomaly_rate": 101},
            {"anomaly_rate": True},
            {"allow_file_faults": "true"},
            {"anomalies": ["wrong_checksum"]},
            {
                "anomalies": ["wrong_checksum"],
                "allow_file_faults": True,
                "destination": "sqlserver",
                "allow_sql_write": True,
            },
            {"anomalies": ["corrupt_image"], "allow_file_faults": True},
            {"anomalies": ["invalid_number", "negative_quantity"]},
            {
                "anomalies": ["extra_column", "missing_column"],
                "allow_file_faults": True,
            },
        ]
        for body in requests:
            with self.subTest(body=body):
                response = self.request("POST", "/jobs", json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "INVALID_REQUEST")
                self.assertNotIn("private-sentinel-unknown", response.text)
        self.generator.assert_not_called()
        self.assertEqual(self.app.state.jobs.jobs, {})

    def test_explicit_file_fault_consent_reaches_the_generator(self):
        accepted = self.request(
            "POST",
            "/jobs",
            json={
                "anomalies": ["corrupt_image"],
                "allow_file_faults": True,
                "images": True,
            },
        )
        self.assertEqual(accepted.status_code, 202)
        result = self.wait_job(accepted.json()["id"])
        self.assertEqual(result["status"], "succeeded")
        options = self.generator.call_args.kwargs
        self.assertEqual(options["anomalies"], ["corrupt_image"])
        self.assertTrue(options["allow_file_faults"])
        self.assertTrue(options["images"])

    def test_intentional_file_fault_generation_succeeds_with_real_sources(
        self,
    ):
        self.app = API.create_app(
            self.private,
            token="unit-api-token",
            landing=self.directory / "real-landing",
        )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        with mock.patch.object(API.httpx, "Client") as outbound:
            response = self.request(
                "POST",
                "/jobs",
                json={
                    "scenario": "clean",
                    "anomalies": ["wrong_checksum"],
                    "allow_file_faults": True,
                },
            )
            self.assertEqual(response.status_code, 202)
            result = self.wait_job(response.json()["id"])
            outbound.assert_not_called()
        self.assertEqual(result["status"], "succeeded")
        generated = result["results"][0]
        diagnostics = generated["diagnostics"]
        self.assertEqual(diagnostics["anomalies"], ["wrong_checksum"])
        self.assertTrue(diagnostics["intentional_file_faults"])
        fault = diagnostics["file_faults"][0]
        source = self.directory / "real-landing" / fault["path"]
        marker = source.with_name(source.name + ".ready")
        self.assertTrue(source.is_file())
        self.assertNotEqual(
            marker.read_text().strip(),
            next(
                item["sha256"]
                for item in generated["sources"]
                if item["path"] == fault["path"]
            ),
        )

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

    def test_source_job_calls_only_the_generator_with_minimal_settings(self):
        with mock.patch.object(API.httpx, "Client") as outbound:
            accepted = self.request("POST", "/jobs", json={}).json()
            result = self.wait_job(accepted["id"])
            outbound.assert_not_called()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["completed"], 1)
        kwargs = self.generator.call_args.kwargs
        self.assertEqual(kwargs["destination"], "landing")
        self.assertFalse(kwargs["allow_sql_write"])
        self.assertNotIn("S3_SECRET_KEY", kwargs["settings"])
        self.assertNotIn("WORKER_TOKEN", kwargs["settings"])

    def test_sql_consent_and_stream_limits_are_validated(self):
        for payload in [
            {"destination": "sqlserver"},
            {"batches": 2},
            {"mode": "stream", "batches": 101},
            {"mode": "stream", "batches": 60, "interval_seconds": 60},
            {"orders": 1001},
            {"mode": "stream", "interval_seconds": 61},
            {"extra": "sentinel-private-input"},
        ]:
            with self.subTest(keys=list(payload)):
                response = self.request("POST", "/jobs", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("sentinel-private-input", response.text)
        self.generator.assert_not_called()

    def test_demo_ca_maps_only_from_the_private_directory(self):
        store = self.app.state.store
        certificate = store.path.parent / "enterprise-ca.pem"
        certificate.write_text("synthetic certificate placeholder")
        store.applied["CONTROL_SETTINGS_DIR"] = "/srv/company/private"
        store.applied["DEMO_SQLSERVER_CA_FILE"] = (
            "/srv/company/private/enterprise-ca.pem"
        )
        mapped = store.source_settings()
        self.assertEqual(mapped["DEMO_SQLSERVER_CA_FILE"], str(certificate))
        self.assertIn("SOURCE_SQLSERVER_DATABASE", mapped)
        self.assertNotIn("S3_SECRET_KEY", mapped)
        store.applied["DEMO_SQLSERVER_CA_FILE"] = "/srv/outside-ca.pem"
        with self.assertRaises(ValueError):
            store.source_settings()
        linked = store.path.parent / "linked-ca.pem"
        linked.symlink_to(certificate)
        store.applied["DEMO_SQLSERVER_CA_FILE"] = (
            "/srv/company/private/linked-ca.pem"
        )
        with self.assertRaises(ValueError):
            store.source_settings()

    def test_job_reports_that_saved_settings_are_not_active(self):
        self.request(
            "PATCH",
            "/settings",
            json={"values": {"TRINO_PORT": "8090"}},
        )
        accepted = self.request("POST", "/jobs", json={}).json()
        self.assertTrue(accepted["pending_restart"])
        result = self.wait_job(accepted["id"])
        self.assertTrue(result["pending_restart"])

    def test_source_failure_is_redacted(self):
        self.generator.side_effect = RuntimeError(
            "sentinel-private-db-password"
        )
        accepted = self.request("POST", "/jobs", json={}).json()
        result = self.wait_job(accepted["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "SOURCE_GENERATION_FAILED")
        self.assertNotIn("sentinel-private-db-password", json.dumps(result))

    def test_finite_stream_can_be_cancelled_between_batches(self):
        accepted = self.request(
            "POST",
            "/jobs",
            json={"mode": "stream", "batches": 3, "interval_seconds": 1},
        ).json()
        deadline = time.monotonic() + 1
        while self.app.state.jobs.get(accepted["id"])["completed"] < 1:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.005)
        self.request("POST", "/jobs/" + accepted["id"] + "/cancel")
        result = self.wait_job(accepted["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed"], 1)

    def test_mutex_rejects_overlapping_jobs_settings_and_pipeline(self):
        release = threading.Event()
        started = threading.Event()
        self.addCleanup(release.set)

        def blocked_generator(*_, **options):
            started.set()
            release.wait(2)
            return {"batch": options["batch"], "sources": []}

        self.generator.side_effect = blocked_generator
        accepted = self.request("POST", "/jobs", json={}).json()
        self.assertTrue(started.wait(1))
        self.assertEqual(
            self.request("POST", "/jobs", json={}).status_code, 409
        )
        self.assertEqual(
            self.request("POST", "/pipeline/verify").status_code, 409
        )
        self.assertEqual(
            self.request(
                "PATCH", "/settings", json={"values": {}}
            ).status_code,
            409,
        )
        release.set()
        self.assertEqual(self.wait_job(accepted["id"])["status"], "succeeded")

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
                409,
            )
            release.set()
            result = self.wait_job(identifier)
        self.assertEqual(result["status"], "succeeded")
        self.assertNotIn("sentinel-private-worker-detail", json.dumps(result))
        self.generator.assert_not_called()
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
