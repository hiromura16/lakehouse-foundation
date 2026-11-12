"""Test portal discovery and bounded reads without a live lakehouse."""

import contextlib
import importlib.util
import io
import runpy
import sys
import tempfile
import unittest
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
        "portal_api", DEMO / "api.py"
    )
    API = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(API)
PORTAL = sys.modules["poc_api"]
QUALITY = sys.modules["poc_quality"]
CATALOG = sys.modules["poc_catalog"]


class PortalApiTests(unittest.TestCase):
    """Use real routing with deterministic service and query dependencies."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="portal-api-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.private = self.directory / "private/site.env"
        with (
            mock.patch.dict(
                sys.modules,
                {"settings": sys.modules["control_settings"].VALIDATOR},
            ),
            mock.patch.object(
                sys, "argv", ["configure.py", "--output", str(self.private)]
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(
                str(ROOT / "scripts/configure.py"), run_name="__main__"
            )
        self.exporter = mock.Mock(return_value={"datasets": []})
        self.reader = mock.Mock(return_value=[])
        self.probe = mock.Mock(return_value={"trino": True})
        self.network = mock.Mock(
            return_value={"checks": [], "configuration": {}}
        )
        with (
            mock.patch.object(PORTAL, "quality_counts", self.reader),
            mock.patch.object(PORTAL, "probe_services", self.probe),
            mock.patch.object(PORTAL, "run_checks", self.network),
        ):
            self.app = API.create_app(
                self.private,
                token="unit-portal-token",
                landing=self.directory / "landing",
                ai_exporter=self.exporter,
            )
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer unit-portal-token"}

    def get(self, path):
        return self.client.get(path, headers=self.headers)

    def test_discovery_is_authenticated_typed_and_has_no_private_source_names(
        self,
    ):
        self.assertEqual(self.client.get("/capabilities").status_code, 401)
        self.app.state.store.applied["SOURCE_SQLSERVER_HOST"] = (
            "private-db-sentinel"
        )
        self.app.state.store.applied["SOURCE_SQLSERVER_PASSWORD"] = (
            "private-db-password"
        )
        capabilities = self.get("/capabilities")
        catalog = self.get("/catalog")
        self.assertEqual(capabilities.status_code, 200)
        self.assertEqual(catalog.status_code, 200)
        items = capabilities.json()["capabilities"]
        self.assertEqual(
            next(x["access"] for x in items if x["id"] == "generate"), "write"
        )
        self.assertEqual(
            next(x["access"] for x in items if x["id"] == "catalog"), "read"
        )
        datasets = catalog.json()["datasets"]
        self.assertEqual(len(datasets), 8)
        production = next(
            item for item in datasets if item["id"] == "production"
        )
        self.assertEqual(
            production["primary_key"], ["factory_id", "production_date"]
        )
        self.assertIn("medallion/03_gold.sql", production["provenance"])
        self.assertNotIn("private-db-", catalog.text)
        schema = self.client.get("/openapi.json").json()
        self.assertIn("Capabilities", schema["components"]["schemas"])
        self.assertIn("Catalog", schema["components"]["schemas"])
        self.probe.assert_not_called()
        self.reader.assert_not_called()

    def test_onboarding_routes_share_auth_without_runtime_clients(self):
        """Exercise the complete application integration, not just a router."""
        self.assertEqual(
            self.client.get("/onboarding/example").status_code, 401
        )
        example = self.get("/onboarding/example")
        self.assertEqual(example.status_code, 200)
        self.assertEqual(len(example.json()["discovery"]["tables"]), 6)
        self.assertFalse(example.json()["database_checked"])
        capabilities = self.get("/capabilities").json()["capabilities"]
        onboarding = [
            item
            for item in capabilities
            if item["id"].startswith("onboarding_")
        ]
        self.assertEqual(len(onboarding), 5)
        self.assertTrue(all(item["access"] == "read" for item in onboarding))
        self.assertIn(
            "/onboarding/plan",
            self.client.get("/openapi.json").json()["paths"],
        )
        self.probe.assert_not_called()
        self.reader.assert_not_called()
        self.network.assert_not_called()

    def test_mcp_settings_use_exact_public_names_and_mask_the_api_token(self):
        response = self.get("/settings")
        original = self.app.state.store.read()["MCP_API_TOKEN"]
        self.assertIn("MCP_API_TOKEN", response.json()["configured_secrets"])
        self.assertNotIn("MCP_API_TOKEN", response.json()["values"])
        self.assertEqual(response.json()["values"]["MCP_PORT"], "8503")
        self.assertEqual(
            response.json()["values"]["MCP_ENABLE_MUTATIONS"], "false"
        )
        self.assertIn("MCP_ALLOWED_HOSTS", response.json()["values"])
        self.assertIn("CORPORATE_CA_FILE", response.json()["values"])
        self.assertNotIn(original, response.text)
        saved = self.client.patch(
            "/settings",
            headers=self.headers,
            json={"values": {"MCP_API_TOKEN": "", "MCP_PORT": "8504"}},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            self.app.state.store.read()["MCP_API_TOKEN"], original
        )
        self.assertEqual(saved.json()["values"]["MCP_PORT"], "8504")
        self.assertFalse(saved.json()["applied"])
        legacy = self.client.patch(
            "/settings",
            headers=self.headers,
            json={"values": {"MCP_TOKEN": "unit-obsolete-name"}},
        )
        self.assertEqual(legacy.status_code, 422)
        self.assertNotIn("unit-obsolete-name", legacy.text)

    def test_mcp_profile_enables_its_gateway_and_control_api_card(self):
        store = self.app.state.store
        store.applied["COMPOSE_PROFILES"] = "mcp"
        self.probe.return_value = {"swagger": True, "mcp": True}
        response = self.get("/services")
        cards = {item["id"]: item for item in response.json()["services"]}
        self.assertEqual(cards["mcp"]["state"], "available")
        self.assertEqual(cards["mcp"]["url"], "http://localhost:8503/mcp")
        self.assertEqual(cards["swagger"]["state"], "available")
        self.assertEqual(cards["swagger"]["profiles"], ["demo", "mcp"])
        self.assertEqual(cards["swagger"]["profile_match"], "any")
        self.assertNotIn(store.applied["MCP_API_TOKEN"], response.text)
        self.assertNotIn(store.applied["CONTROL_API_TOKEN"], response.text)

    def test_capabilities_separate_mcp_transport_and_control_api_profiles(
        self,
    ):
        response = self.get("/capabilities")
        capabilities = {
            item["id"]: item for item in response.json()["capabilities"]
        }
        gateway = capabilities["mcp_transport"]
        self.assertEqual(gateway["service"], "mcp")
        self.assertEqual(gateway["path"], "/mcp")
        self.assertEqual(gateway["required_profiles"], ["mcp"])
        self.assertEqual(gateway["access"], "read")
        self.assertEqual(capabilities["catalog"]["service"], "control_api")
        self.assertEqual(
            capabilities["catalog"]["required_profiles"], ["demo", "mcp"]
        )
        self.assertEqual(capabilities["catalog"]["profile_match"], "any")
        self.app.state.store.applied["MCP_ENABLE_MUTATIONS"] = "true"
        response = self.get("/capabilities")
        gateway = next(
            item
            for item in response.json()["capabilities"]
            if item["id"] == "mcp_transport"
        )
        self.assertEqual(gateway["access"], "write")

    def test_compute_cards_use_separate_native_ports_and_profile_states(self):
        public = self.get("/settings").json()["values"]
        self.assertEqual(public["SPARK_MASTER_UI_PORT"], "8082")
        self.assertEqual(public["SPARK_WORKER_UI_PORT"], "8085")
        self.assertEqual(public["SPARK_HISTORY_UI_PORT"], "18080")
        store = self.app.state.store
        store.applied.update(
            {
                "COMPOSE_PROFILES": "compute",
                "SPARK_MASTER_UI_PORT": "18082",
                "SPARK_WORKER_UI_PORT": "18085",
                "SPARK_HISTORY_UI_PORT": "18081",
            }
        )
        self.probe.return_value = {"spark-master": True, "spark-history": True}
        response = self.get("/services")
        cards = {item["id"]: item for item in response.json()["services"]}
        self.assertEqual(cards["spark-master"]["state"], "available")
        self.assertEqual(cards["spark-worker"]["state"], "unreachable")
        self.assertEqual(cards["spark-history"]["state"], "available")
        self.assertEqual(
            cards["spark-master"]["url"], "http://localhost:18082/"
        )
        self.assertEqual(
            cards["spark-worker"]["url"], "http://localhost:18085/"
        )
        self.assertEqual(
            cards["spark-history"]["url"], "http://localhost:18081/"
        )
        store.applied["COMPOSE_PROFILES"] = "demo"
        cards = {
            item["id"]: item
            for item in self.get("/services").json()["services"]
        }
        self.assertTrue(
            all(
                cards[key]["state"] == "not_enabled"
                for key in ("spark-master", "spark-worker", "spark-history")
            )
        )
        self.assertTrue(
            all(
                cards[key]["profile"] == "compute"
                for key in ("spark-master", "spark-worker", "spark-history")
            )
        )

    def test_n8n_ssrf_settings_are_editable_without_proxy_or_runtime_changes(
        self,
    ):
        updates = {
            "N8N_SSRF_PROTECTION_ENABLED": "true",
            "N8N_SSRF_BLOCKED_IP_RANGES": "default,100.64.0.0/10",
            "N8N_SSRF_ALLOWED_IP_RANGES": "",
            "N8N_SSRF_ALLOWED_HOSTNAMES": "worker",
            "N8N_SSRF_BLOCKED_HOSTNAMES": "",
            "N8N_SSRF_DNS_CACHE_MAX_SIZE": "1048576",
        }
        proxy = self.app.state.store.read()["NO_PROXY"]
        response = self.client.patch(
            "/settings", headers=self.headers, json={"values": updates}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["applied"])
        self.assertTrue(
            all(
                response.json()["values"][key] == value
                for key, value in updates.items()
            )
        )
        self.assertEqual(self.app.state.store.read()["NO_PROXY"], proxy)
        self.network.assert_not_called()
        self.reader.assert_not_called()
        help_slugs = {
            item["slug"] for item in self.get("/help").json()["documents"]
        }
        self.assertIn("ssrf", help_slugs)

    def test_preview_is_bounded_and_uses_only_the_fixed_gold_exporter(self):
        response = self.get("/catalog/preview?dataset=sales&limit=12")
        self.assertEqual(response.status_code, 200)
        self.exporter.assert_called_once_with(["sales"], 12)
        self.assertEqual(
            self.get("/catalog/preview?limit=501").status_code, 422
        )
        self.assertEqual(
            self.get("/catalog/preview?dataset=private").status_code, 422
        )
        self.exporter.side_effect = ValueError("private-sql-driver-details")
        response = self.get("/catalog/preview")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("private-sql", response.text)

    def test_service_cards_distinguish_missing_config_and_reachability(
        self,
    ):
        store = self.app.state.store
        store.applied["COMPOSE_PROFILES"] = "demo,bi,vector"
        store.applied["SUPERSET_DB_PASSWORD"] = "unit-db-secret"
        store.applied["SUPERSET_SECRET_KEY"] = "unit-app-secret"
        store.applied["SUPERSET_ADMIN_PASSWORD"] = "unit-admin-secret"
        store.applied["SUPERSET_REDIS_PASSWORD"] = "unit-redis-secret"
        # This case intentionally lacks vector keys despite fresh defaults.
        store.applied["QDRANT_API_KEY"] = ""
        store.applied["QDRANT_READ_ONLY_API_KEY"] = ""
        response = self.get("/services")
        self.assertEqual(response.status_code, 200)
        cards = {item["id"]: item for item in response.json()["services"]}
        self.assertEqual(cards["trino"]["state"], "available")
        self.assertEqual(cards["superset"]["state"], "unreachable")
        self.assertEqual(cards["qdrant"]["state"], "unconfigured")
        self.assertEqual(cards["jupyter"]["state"], "not_enabled")
        self.assertEqual(cards["jupyter"]["url"], "http://localhost:8888/lab")
        self.assertNotIn("unit-db-secret", response.text)
        self.assertNotIn("unit-app-secret", response.text)
        self.assertNotIn("?token=", response.text)
        self.assertNotIn("@localhost", response.text)

    def test_help_is_allowlisted_and_serves_offline_diagrams(self):
        listing = self.get("/help").json()
        self.assertIn("proxy", {item["slug"] for item in listing["documents"]})
        response = self.get("/help/proxy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("proxy", response.json()["markdown"].lower())
        self.assertEqual(self.get("/help/site.env").status_code, 404)
        diagram = self.get("/help/assets/architecture")
        self.assertEqual(diagram.status_code, 200)
        self.assertIn("<svg", diagram.json()["content"])
        self.assertNotIn('href="https://', diagram.json()["content"])
        self.assertNotIn(str(ROOT), response.text)
        self.assertEqual(self.get("/help/assets/site.env").status_code, 404)

    def test_help_rejects_symlinks_and_large_files(self):
        docs = self.directory / "docs"
        docs.mkdir()
        selected = docs / "PROXY.md"
        selected.symlink_to(self.private)
        with mock.patch.object(PORTAL, "ROOT", self.directory):
            response = self.get("/help/proxy")
            self.assertEqual(response.status_code, 503)
            selected.unlink()
            selected.write_bytes(b"x" * (256 * 1024 + 1))
            self.assertEqual(self.get("/help/proxy").status_code, 503)
        self.assertNotIn("CONTROL_API_TOKEN=", response.text)

    def test_quality_error_is_sanitized_and_counts_are_limited(self):
        self.reader.return_value = [
            {
                "dataset": "mes_events",
                "bronze_rows": 20,
                "silver_rows": 18,
                "quarantine_rows": 1,
            }
        ]
        response = self.get("/quality")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["consistent_snapshot"])
        self.reader.side_effect = ValueError("private-catalog-sql-error")
        response = self.get("/quality")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("private-catalog", response.text)

    def test_recent_jobs_are_bounded_snapshots_and_do_not_restart_work(self):
        self.app.state.jobs.jobs = {
            "one": {"id": "one", "status": "succeeded", "results": []},
            "two": {"id": "two", "status": "failed", "results": []},
        }
        response = self.get("/jobs?limit=1")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["persistent"])
        self.assertEqual(response.json()["jobs"][0]["id"], "two")
        self.assertEqual(self.get("/jobs?limit=21").status_code, 422)
        snapshot = self.app.state.jobs.history(1)
        snapshot[0]["status"] = "changed"
        self.assertEqual(self.app.state.jobs.jobs["two"]["status"], "failed")
        self.assertFalse(self.app.state.jobs.operation.locked())

    def test_network_checks_are_fixed_scoped_and_do_not_expose_proxy_values(
        self,
    ):
        store = self.app.state.store
        proposed_proxy = "http://user:private-proxy@proxy.example.invalid:3128"
        store.save({"HTTPS_PROXY": proposed_proxy})
        report = self.get("/network/config?use_saved=true")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.json()["configuration_scope"], "saved")
        self.assertTrue(report.json()["pending_restart"])
        self.assertNotIn("private-proxy", report.text)
        self.assertNotIn("proxy.example.invalid", report.text)
        for body in (
            {"targets": ["arbitrary-url"]},
            {"targets": ["pypi", "pypi"]},
            {"targets": ["pypi"], "url": "http://private-sentinel"},
        ):
            response = self.client.post(
                "/network/checks", headers=self.headers, json=body
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("private-sentinel", response.text)
        self.network.assert_not_called()
        response = self.client.post(
            "/network/checks",
            headers=self.headers,
            json={"targets": ["pypi"], "use_saved": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.network.call_args.kwargs["timeout"], 5)
        self.assertEqual(self.network.call_args.args[0]["HTTPS_PROXY"], "")
        response = self.client.post(
            "/network/checks",
            headers=self.headers,
            json={"targets": ["pypi"], "use_saved": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "private-proxy", self.network.call_args.args[0]["HTTPS_PROXY"]
        )

    def test_corporate_ca_maps_only_inside_the_private_directory(
        self,
    ):
        certificate = self.private.parent / "corporate.pem"
        certificate.write_text("public-certificate-placeholder")
        store = self.app.state.store
        store.applied["CONTROL_SETTINGS_DIR"] = "/srv/company/private"
        store.applied["CORPORATE_CA_FILE"] = (
            "/srv/company/private/corporate.pem"
        )
        response = self.client.post(
            "/network/checks", headers=self.headers, json={"targets": ["pypi"]}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.network.call_args.args[0]["CORPORATE_CA_FILE"],
            str(certificate),
        )
        self.network.reset_mock()
        for selected in (
            "/srv/outside.pem",
            "/srv/company/private/missing.pem",
        ):
            store.applied["CORPORATE_CA_FILE"] = selected
            response = self.client.post(
                "/network/checks",
                headers=self.headers,
                json={"targets": ["pypi"]},
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["code"], "NETWORK_CA_UNAVAILABLE")
            self.assertNotIn(selected, response.text)
        linked = self.private.parent / "linked.pem"
        linked.symlink_to(certificate)
        store.applied["CORPORATE_CA_FILE"] = "/srv/company/private/linked.pem"
        response = self.client.post(
            "/network/checks", headers=self.headers, json={"targets": ["pypi"]}
        )
        self.assertEqual(response.status_code, 422)
        self.network.assert_not_called()


class FixedServiceProbeTests(unittest.TestCase):
    """Keep readiness checks on known enabled origins and hide their keys."""

    def test_probe_never_uses_an_external_url_or_outbound_proxy(self):
        services = sys.modules["poc_services"]
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        values = {
            "COMPOSE_PROFILES": "notebooks,vector",
            "JUPYTER_TOKEN": "unit-notebook-token",
            "NOTEBOOKS_DIR": "/srv/notebooks",
            "QDRANT_API_KEY": "unit-vector-admin",
            "QDRANT_READ_ONLY_API_KEY": "unit-vector-reader",
        }
        with mock.patch.object(
            services.httpx, "Client", return_value=client
        ) as factory:
            result = services.probe_services(values)
        self.assertTrue(result["jupyter"])
        self.assertTrue(result["qdrant"])
        allowed = {
            definition.health_url
            for definition in CATALOG.SERVICES
            if definition.id in {"trino", "catalog", "jupyter", "qdrant"}
        }
        self.assertEqual(
            {call.args[0] for call in client.get.call_args_list}, allowed
        )
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertFalse(factory.call_args.kwargs["follow_redirects"])
        rendered = services.service_cards(values, result)
        self.assertNotIn("unit-notebook-token", str(rendered))
        self.assertNotIn("unit-vector-admin", str(rendered))
        self.assertNotIn("unit-vector-reader", str(rendered))

    def test_fixed_cdc_cards_require_source_reader_without_writer_auth(self):
        """Fixed mode readiness must not depend on generic CDC credentials."""
        services = sys.modules["poc_services"]
        values = {
            "COMPOSE_PROFILES": "cdc",
            "CDC_SOURCE_MODE": "fixed-demo",
            "SOURCE_SQLSERVER_HOST": "fixture.example.invalid",
            "SOURCE_SQLSERVER_DATABASE": "fixture_database",
            "SOURCE_SQLSERVER_USER": "fixture_reader",
            "SOURCE_SQLSERVER_PASSWORD": "fixture-reader-secret",
            "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
            "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
        }
        cards = services.service_cards(values, {"kafbat": True})
        selected = next(card for card in cards if card["id"] == "kafbat")
        self.assertEqual(selected["state"], "available")
        self.assertNotIn("fixture-reader-secret", str(cards))
        values.pop("SOURCE_SQLSERVER_PASSWORD")
        cards = services.service_cards(values, {"kafbat": True})
        selected = next(card for card in cards if card["id"] == "kafbat")
        self.assertEqual(selected["state"], "unconfigured")
        self.assertEqual(
            selected["missing_settings"], ["SOURCE_SQLSERVER_PASSWORD"]
        )

    def test_compute_probes_only_the_three_fixed_spark_ui_origins(self):
        services = sys.modules["poc_services"]
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value.status_code = 200
        with mock.patch.object(services.httpx, "Client", return_value=client):
            health = services.probe_services({"COMPOSE_PROFILES": "compute"})
        self.assertTrue(
            all(
                health[key]
                for key in ("spark-master", "spark-worker", "spark-history")
            )
        )
        called = {call.args[0] for call in client.get.call_args_list}
        self.assertEqual(
            called,
            {
                "http://trino:8080/v1/info",
                "http://catalog:8181/health",
                "http://spark-master:8080/",
                "http://spark-worker:8081/",
                "http://spark-history:18080/",
            },
        )


class FixedQualityQueryTests(unittest.TestCase):
    """Validate fixed query, result origin and unfinished cancellation."""

    def driver(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        return client

    def response(self, payload):
        result = mock.Mock(status_code=200, content=b"bounded")
        result.json.return_value = payload
        return result

    def test_fixed_counts_return_only_six_known_datasets(self):
        client = self.driver()
        client.post.return_value = self.response(
            {"data": [[key, 5, 4, 1] for key in CATALOG.SOURCE_DETAILS]}
        )
        with mock.patch.object(QUALITY.httpx, "Client", return_value=client):
            result = QUALITY.quality_counts()
        self.assertEqual(len(result), 6)
        self.assertEqual(
            client.post.call_args.args[0], "http://trino:8080/v1/statement"
        )
        self.assertEqual(
            client.post.call_args.kwargs["content"], QUALITY.statement()
        )
        self.assertNotIn("INSERT", QUALITY.statement())
        client.delete.assert_not_called()

    def test_unfinished_read_is_cancelled_without_exposing_transport_error(
        self,
    ):
        client = self.driver()
        pending = "http://trino:8080/v1/statement/queued/owned/1"
        client.post.return_value = self.response({"nextUri": pending})
        client.get.side_effect = QUALITY.httpx.ReadTimeout(
            "private-quality-error"
        )
        with mock.patch.object(QUALITY.httpx, "Client", return_value=client):
            with self.assertRaises(QUALITY.QualityUnavailable) as error:
                QUALITY.quality_counts()
        self.assertNotIn("private-quality", str(error.exception))
        client.delete.assert_called_once_with(pending, timeout=2)

    def test_untrusted_result_references_are_never_followed(self):
        for reference in (
            "http://outside.invalid/v1/statement/private",
            "http://trino:8080/v1/statement/../query/other",
            "http://trino:8080/v1/statement/%2e%2e/query/other",
            "http://trino:8080/v1/statement/query?redirect=other",
        ):
            with self.subTest(reference=reference):
                client = self.driver()
                client.post.return_value = self.response(
                    {"nextUri": reference}
                )
                with mock.patch.object(
                    QUALITY.httpx, "Client", return_value=client
                ):
                    with self.assertRaises(QUALITY.QualityUnavailable):
                        QUALITY.quality_counts()
                client.get.assert_not_called()
                client.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
