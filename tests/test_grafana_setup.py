"""Keep GUI dashboard state authoritative without network or Docker access."""

import base64
import contextlib
import importlib.util
import io
import json
import os
import runpy
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "grafana_settings", ROOT / "scripts/settings.py"
)
SETTINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETTINGS)
SPEC = importlib.util.spec_from_file_location(
    "grafana_setup_subject", ROOT / "scripts/grafana_setup.py"
)
GRAFANA = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {"settings": SETTINGS}):
    SPEC.loader.exec_module(GRAFANA)

VALUES = {
    "BIND_IP": "127.0.0.1",
    "GRAFANA_PORT": "3000",
    "GRAFANA_ADMIN_PASSWORD": "synthetic-" + "credential",
}
HEALTH = (200, {"database": "ok"})
EXISTING = {
    "metadata": {"name": GRAFANA.DASHBOARD_UID},
    "spec": {"title": "Saved in GUI", "panels": [], "version": 9},
}


class GrafanaSetupTests(unittest.TestCase):
    """Prove create-only API semantics and private export behavior."""

    def test_template_uses_new_api_and_remains_editable(self):
        """Wrap the classic model without stored IDs or stale versions."""
        payload = GRAFANA.create_payload()
        self.assertEqual(payload["apiVersion"], "dashboard.grafana.app/v1")
        self.assertEqual(payload["metadata"]["name"], "lakehouse-operations")
        self.assertTrue(payload["spec"]["editable"])
        self.assertNotIn("uid", payload["spec"])
        self.assertNotIn("version", payload["spec"])
        self.assertTrue(payload["spec"]["panels"])

    def test_missing_uid_is_created_once_with_post(self):
        """Create a missing resource without using update or legacy routes."""
        api = mock.Mock()
        api.request.side_effect = [HEALTH, (404, None), (201, EXISTING)]
        with mock.patch.object(GRAFANA, "GrafanaAPI", return_value=api):
            result = GRAFANA.setup(VALUES)
        self.assertEqual(result["status"], "created")
        methods = [call.args[0] for call in api.request.call_args_list]
        self.assertEqual(methods, ["GET", "GET", "POST"])
        self.assertEqual(
            api.request.call_args_list[-1].args[1], GRAFANA.DASHBOARDS_API
        )

    def test_gui_state_is_not_replaced_or_even_loaded_from_template(self):
        """An existing UID wins even if the repository template changes."""
        api = mock.Mock()
        api.request.side_effect = [HEALTH, (200, EXISTING)]
        with (
            mock.patch.object(GRAFANA, "GrafanaAPI", return_value=api),
            mock.patch.object(
                GRAFANA, "create_payload", side_effect=AssertionError
            ),
        ):
            result = GRAFANA.setup(VALUES)
        self.assertEqual(result["status"], "exists")
        self.assertEqual(api.request.call_count, 2)
        self.assertEqual(EXISTING["spec"]["title"], "Saved in GUI")

    def test_lookup_failures_cannot_become_overwriting_writes(self):
        """Authentication and server errors stop without any POST or PUT."""
        for code in [401, 403, 500]:
            with self.subTest(code=code):
                api = mock.Mock()
                api.request.side_effect = [HEALTH, (code, None)]
                with (
                    mock.patch.object(GRAFANA, "GrafanaAPI", return_value=api),
                    self.assertRaises(GRAFANA.GrafanaError),
                ):
                    GRAFANA.setup(VALUES)
                self.assertEqual(api.request.call_count, 2)

    def test_create_race_is_read_back_without_an_update(self):
        """A concurrent creator may win; its saved dashboard stays intact."""
        api = mock.Mock()
        api.request.side_effect = [
            HEALTH,
            (404, None),
            (409, None),
            (200, EXISTING),
        ]
        with mock.patch.object(GRAFANA, "GrafanaAPI", return_value=api):
            result = GRAFANA.setup(VALUES)
        self.assertEqual(result["status"], "exists")
        self.assertEqual(
            [call.args[0] for call in api.request.call_args_list],
            ["GET", "GET", "POST", "GET"],
        )

    def test_readiness_retries_transport_and_honors_deadline(self):
        """Retry startup failures while bounding how long startup can wait."""
        api = mock.Mock()
        api.request.side_effect = [
            GRAFANA.GrafanaUnavailable("Not ready."),
            HEALTH,
        ]
        with mock.patch.object(GRAFANA.time, "sleep"):
            GRAFANA.wait_ready(api)
        self.assertEqual(api.request.call_count, 2)
        api.request.side_effect = None
        api.request.return_value = (503, None)
        with (
            mock.patch.object(
                GRAFANA.time, "monotonic", side_effect=[10, 10, 12, 12]
            ),
            mock.patch.object(GRAFANA.time, "sleep"),
            self.assertRaisesRegex(GRAFANA.GrafanaError, "deadline"),
        ):
            GRAFANA.wait_ready(api, timeout=1)

    def test_basic_auth_is_in_headers_and_proxy_redirects_are_disabled(self):
        """Keep credentials out of URLs, proxy requests, and redirects."""
        response = io.BytesIO(json.dumps({"database": "ok"}).encode())
        response.status = 200
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(
            GRAFANA.urllib.request, "build_opener", return_value=opener
        ) as factory:
            api = GRAFANA.GrafanaAPI(VALUES)
            api.request("GET", "/api/health")
        request = opener.open.call_args.args[0]
        expected = base64.b64encode(
            ("admin:" + VALUES["GRAFANA_ADMIN_PASSWORD"]).encode()
        ).decode()
        self.assertEqual(
            request.get_header("Authorization"), "Basic " + expected
        )
        self.assertNotIn(VALUES["GRAFANA_ADMIN_PASSWORD"], request.full_url)
        self.assertEqual(factory.call_args.args[0].proxies, {})
        self.assertIsInstance(factory.call_args.args[1], GRAFANA.NoRedirect)

    def test_export_reads_gui_model_and_creates_a_private_external_file(self):
        """Export current GUI state without modifying any Grafana resource."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "dashboard.json"
            api = mock.Mock()
            api.request.side_effect = [HEALTH, (200, EXISTING)]
            with mock.patch.object(GRAFANA, "GrafanaAPI", return_value=api):
                result = GRAFANA.export_dashboard(VALUES, output)
            self.assertEqual(result["status"], "exported")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            model = json.loads(output.read_text())
            self.assertEqual(model["title"], "Saved in GUI")
            self.assertEqual(model["version"], 9)
            self.assertEqual(model["uid"], GRAFANA.DASHBOARD_UID)
            self.assertEqual(
                [call.args[0] for call in api.request.call_args_list],
                ["GET", "GET"],
            )

    def test_export_rejects_source_paths_and_existing_files(self):
        """Never export private GUI settings into Git or overwrite a review."""
        with mock.patch.object(GRAFANA, "GrafanaAPI") as api:
            with self.assertRaises(GRAFANA.GrafanaError):
                GRAFANA.export_dashboard(VALUES, ROOT / "dashboard.json")
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "dashboard.json"
                output.write_text("Preserved review.")
                with self.assertRaises(GRAFANA.GrafanaError):
                    GRAFANA.export_dashboard(VALUES, output)
                self.assertEqual(output.read_text(), "Preserved review.")
            api.assert_not_called()

    def test_compose_keeps_only_datasource_provisioning(self):
        """The GUI dashboard model cannot be reloaded by a file provider."""
        compose = (ROOT / "infra/lakehouse/compose.yaml").read_text()
        self.assertIn("source: grafana-datasource", compose)
        self.assertNotIn("grafana-dashboard-provider", compose)
        self.assertNotIn("source: grafana-dashboard", compose)
        self.assertFalse(
            (ROOT / "infra/monitoring/grafana-dashboard-provider.yml").exists()
        )


class GrafanaControlTests(unittest.TestCase):
    """Check profile-aware startup and private environment selection."""

    def invoke(self, action, profiles):
        """Run the real control parser with fake Docker and Grafana calls."""
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            path = parent / "site.env"
            configure = ROOT / "scripts/configure.py"
            with (
                mock.patch.dict(sys.modules, {"settings": SETTINGS}),
                mock.patch.object(
                    sys, "argv", [str(configure), "--output", str(path)]
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(str(configure), run_name="__main__")
            landing = parent / "landing"
            landing.mkdir()
            lines = []
            for line in path.read_text().splitlines():
                if line.startswith("LANDING_DIR="):
                    line = "LANDING_DIR=" + str(landing)
                if line.startswith("COMPOSE_PROFILES="):
                    line = "COMPOSE_PROFILES=" + profiles
                lines.append(line)
            path.write_text("\n".join(lines) + "\n")
            setup = mock.Mock(return_value={"status": "exists"})
            fake_module = types.SimpleNamespace(setup=setup)
            control = ROOT / "scripts/control.py"
            with (
                mock.patch.dict(
                    sys.modules,
                    {"settings": SETTINGS, "grafana_setup": fake_module},
                ),
                mock.patch.dict(os.environ, {"LAKEHOUSE_ENV_FILE": str(path)}),
                mock.patch.object(sys, "argv", [str(control), action]),
                mock.patch(
                    "subprocess.run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as docker,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                runpy.run_path(str(control), run_name="__main__")
            return setup.call_count, docker.call_count

    def test_up_initializes_only_with_monitoring_profile(self):
        """Core-only and automation-only startup do not contact Grafana."""
        for profiles, expected in [
            ("", 0),
            ("automation", 0),
            ("monitoring", 1),
        ]:
            with self.subTest(profiles=profiles):
                calls, docker_calls = self.invoke("up", profiles)
                self.assertEqual(calls, expected)
                self.assertEqual(docker_calls, 2)

    def test_explicit_init_uses_env_fallback_without_docker_mutations(self):
        """The explicit action retains shell-selected private file support."""
        calls, docker_calls = self.invoke("grafana-init", "monitoring")
        self.assertEqual(calls, 1)
        self.assertEqual(docker_calls, 0)


if __name__ == "__main__":
    unittest.main()
