"""Verify source-only monitoring contracts; no exporter or PromQL is run."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import grafana_setup as grafana  # noqa: E402

SPEC = importlib.util.spec_from_file_location(
    "monitoring_assets", ROOT / "scripts/monitoring_assets.py"
)
ASSETS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSETS)


class MonitoringFoundationTests(unittest.TestCase):
    """Guard minimal source permissions and continuity of existing metrics."""

    def setUp(self):
        self.compose = yaml.safe_load(
            (ROOT / "infra/lakehouse/compose.yaml").read_text()
        )
        self.services = self.compose["services"]

    def test_exporters_are_internal_and_do_not_inherit_proxy_or_socket(self):
        """Keep host observation explicit and operator approved."""
        for name in ("node-exporter", "cadvisor"):
            with self.subTest(name=name):
                service = self.services[name]
                self.assertEqual(service["profiles"], ["monitoring"])
                self.assertNotIn("ports", service)
                self.assertNotIn("network_mode", service)
                self.assertNotIn("privileged", service)
                self.assertNotIn("devices", service)
                self.assertNotIn("cap_add", service)
                self.assertEqual(service["cap_drop"], ["ALL"])
                self.assertEqual(service["user"], "65534:65534")
                self.assertTrue(service["read_only"])
                self.assertNotIn("environment", service)
                self.assertTrue(service["healthcheck"]["test"])
                for volume in service["volumes"]:
                    self.assertTrue(volume["read_only"])
                    self.assertFalse(volume["bind"]["create_host_path"])
                    self.assertNotIn("docker.sock", str(volume))
                self.assertEqual(
                    service["logging"]["options"],
                    {"max-size": "10m", "max-file": "3"},
                )

    def test_node_namespace_gaps_are_explicit_not_fake_host_network(self):
        """No unsupported netlink, D-Bus or network-namespace shortcut."""
        node = self.services["node-exporter"]
        self.assertEqual(node["pid"], "host")
        command = node["command"]
        self.assertIn("--collector.disable-defaults", command)
        for name in ("netdev", "netstat", "sockstat", "systemd"):
            self.assertNotIn("--collector." + name, command)
        self.assertIn("--path.procfs=/host/proc", command)
        self.assertIn("--collector.processes", command)

    def test_cadvisor_allowlist_does_not_export_all_container_labels(self):
        """The official flag requires store_container_labels=false."""
        command = self.services["cadvisor"]["command"]
        self.assertIn("--store_container_labels=false", command)
        self.assertIn("--docker_only=false", command)
        self.assertNotIn("--env_metadata_whitelist", str(command))
        self.assertNotIn("/var/run", str(self.services["cadvisor"]["volumes"]))

    def test_locks_match_exact_official_exporter_pins(self):
        """Source provenance is separate from actual image execution."""
        locks = json.loads(
            (ROOT / "infra/lakehouse/images.lock.json").read_text()
        )
        for name in ("node-exporter", "cadvisor"):
            pin = self.services[name]["image"]
            self.assertEqual(pin, locks[name]["pinned"])
            self.assertRegex(pin, r"@sha256:[0-9a-f]{64}$")
            self.assertIn("github.com", locks[name]["provenance"])

    def test_metrics_jobs_keep_existing_targets_without_health_dependencies(
        self,
    ):
        """Exporter outages never gate the existing Prometheus lifecycle."""
        config = yaml.safe_load(
            (ROOT / "infra/monitoring/prometheus.yml").read_text()
        )
        jobs = config["scrape_configs"]
        names = [job["job_name"] for job in jobs]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(
            {"prometheus", "lakehouse", "node", "cadvisor"} <= set(names)
        )
        self.assertEqual(config["global"]["scrape_interval"], "15s")
        self.assertEqual(config["global"]["scrape_timeout"], "10s")
        self.assertNotIn("depends_on", self.services["prometheus"])
        self.assertNotIn("depends_on", self.services["grafana"])
        self.assertIn(
            "--storage.tsdb.retention.time=15d",
            self.services["prometheus"]["command"],
        )

    def test_alerts_distinguish_averaging_and_trino_failure_evidence(self):
        """This checks policy text, not PromQL execution or metric emission."""
        rules = yaml.safe_load(
            (ROOT / "infra/monitoring/rules/foundation.yml").read_text()
        )
        alerts = {
            r["alert"]: r
            for group in rules["groups"]
            for r in group["rules"]
            if "alert" in r
        }
        self.assertIn("[5m]", alerts["HostCpuWarning"]["expr"])
        self.assertIn("[10m]", alerts["HostCpuCritical"]["expr"])
        self.assertEqual(alerts["ExporterDownWarning"]["for"], "2m")
        self.assertEqual(alerts["ExporterDownCritical"]["for"], "5m")
        self.assertIn(
            'service!="trino"', alerts["ContainerMemoryWarning"]["expr"]
        )
        self.assertIn(
            "container_oom_events_total",
            alerts["TrinoMemoryFailureEvidence"]["expr"],
        )
        self.assertIn(
            "container_start_time_seconds",
            alerts["TrinoMemoryFailureEvidence"]["expr"],
        )

    def test_generated_dashboards_have_unique_ids_and_no_external_plugins(
        self,
    ):
        """Selectors use real labels and unavailable signals have notices."""
        models = ASSETS.templates()
        self.assertEqual(len({m["uid"] for m in models.values()}), len(models))
        for filename, model in models.items():
            with self.subTest(filename=filename):
                committed = json.loads(
                    (
                        ROOT / "infra/monitoring/dashboards" / filename
                    ).read_text()
                )
                self.assertEqual(committed, model)
                ids = [p["id"] for p in model["panels"]]
                self.assertEqual(len(ids), len(set(ids)))
                for panel in model["panels"]:
                    self.assertIn(panel["type"], {"timeseries", "text"})
                    if "datasource" in panel:
                        self.assertEqual(
                            panel["datasource"]["uid"], "foundation-prometheus"
                        )
                self.assertTrue(
                    any(p["type"] == "text" for p in model["panels"])
                )


class MultipleDashboardTests(unittest.TestCase):
    """Exercise independent create-only UIDs, races and partial failures."""

    def invoke(self, responses):
        api = mock.Mock()
        api.request.side_effect = [(200, {"database": "ok"}), *responses]
        with mock.patch.object(grafana, "GrafanaAPI", return_value=api):
            result = grafana.setup({"COMPOSE_PROFILES": "monitoring"})
        return result, api

    def test_all_first_create_and_all_repeated_exists(self):
        count = len(grafana.TEMPLATES)
        result, api = self.invoke([(404, None), (201, {})] * count)
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(result["dashboards"]), count)
        self.assertFalse(
            any(
                c.args[0] in {"PUT", "PATCH", "DELETE"}
                for c in api.request.call_args_list
            )
        )
        result, api = self.invoke([(200, {})] * count)
        self.assertEqual(result["status"], "exists")
        self.assertTrue(
            all(c.args[0] == "GET" for c in api.request.call_args_list)
        )

    def test_mixed_existing_missing_and_race_keep_gui_state(self):
        result, api = self.invoke(
            [
                (200, {}),
                (404, None),
                (201, {}),
                (404, None),
                (409, None),
                (200, {}),
            ]
        )
        self.assertEqual(
            [r["status"] for r in result["dashboards"]],
            ["exists", "created", "exists"],
        )
        self.assertEqual(
            sum(c.args[0] == "POST" for c in api.request.call_args_list), 2
        )

    def test_partial_failure_continues_without_error_body_leak(self):
        result, _ = self.invoke([(200, {}), (500, None), (200, {})])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            [r["status"] for r in result["dashboards"]],
            ["exists", "failed", "exists"],
        )
        self.assertNotIn("error", json.dumps(result))

    def test_arbitrary_export_uid_rejected_before_network(self):
        with mock.patch.object(grafana, "GrafanaAPI") as api:
            with self.assertRaises(grafana.GrafanaError):
                grafana.export_dashboard({}, "/tmp/unused.json", "unrelated")
            api.assert_not_called()


class MonitoringPathAndValidatorTests(unittest.TestCase):
    """Check private mount boundaries and disposable-validator isolation."""

    def test_monitoring_mount_rejects_protected_and_reused_paths(self):
        import settings

        values = {
            "COMPOSE_PROFILES": "monitoring",
            "LANDING_DIR": "/srv/lab/landing",
            "NOTEBOOKS_DIR": "/srv/lab/notebooks",
        }
        for path in (
            "relative",
            "/",
            "/etc",
            "/tmp",
            "/srv/lab",
            "/srv/lab/notebooks",
            str(ROOT),
            "/srv/../etc",
        ):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    settings.validate_monitoring_paths(
                        dict(values, CADVISOR_DOCKER_DATA_ROOT=path),
                        "/srv/lab/private/site.env",
                    )
        settings.validate_monitoring_paths(values, "/srv/lab/private/site.env")
        settings.validate_monitoring_paths(
            {"COMPOSE_PROFILES": "", "CADVISOR_DOCKER_DATA_ROOT": "/"},
            "/srv/lab/private/site.env",
        )

    def test_mount_normal_file_and_symlink_rejected_without_mutation(self):
        import tempfile

        import settings

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            directory = base / "docker-root"
            directory.mkdir()
            file = base / "normal-file"
            file.write_bytes(b"unchanged")
            link = base / "link"
            link.symlink_to(directory, target_is_directory=True)
            values = {"COMPOSE_PROFILES": "monitoring"}
            for bad in (file, link, link / "child"):
                with self.assertRaises(ValueError):
                    settings.validate_monitoring_paths(
                        dict(values, CADVISOR_DOCKER_DATA_ROOT=str(bad)),
                        base / "private/site.env",
                    )
            settings.validate_monitoring_paths(
                dict(values, CADVISOR_DOCKER_DATA_ROOT=str(directory)),
                base / "private/site.env",
            )
            self.assertEqual(file.read_bytes(), b"unchanged")

    def test_validator_copies_only_public_files_and_grants_no_host_access(
        self,
    ):
        import os
        import stat
        import subprocess

        import validate_observability as validator

        calls = []

        def fake(command, **_kwargs):
            calls.append(command)
            if command[1] == "create":
                self.assertIn("--network", command)
                self.assertIn("none", command)
                self.assertIn("--cap-drop", command)
                self.assertIn("--user", command)
                self.assertNotIn("--read-only", command)
                self.assertFalse(
                    any("--volume" in c or c == "--mount" for c in command)
                )
                return subprocess.CompletedProcess(command, 0, b"a" * 64)
            if command[1] == "cp":
                actual = {
                    str(p.relative_to(command[2]))
                    for p in Path(command[2]).rglob("*")
                    if p.is_file()
                }
                for copied in Path(command[2]).rglob("*"):
                    self.assertEqual(
                        stat.S_IMODE(copied.stat().st_mode),
                        0o755 if copied.is_dir() else 0o644,
                    )
                self.assertEqual(
                    actual,
                    {
                        "prometheus.yml",
                        "rules/foundation.yml",
                        "rules/tests.yml",
                    },
                )
            return subprocess.CompletedProcess(command, 0)

        previous = os.umask(0o077)
        try:
            with mock.patch.object(
                validator.subprocess, "run", side_effect=fake
            ):
                result = validator.execute(validator.jobs()[0])
        finally:
            os.umask(previous)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(
            [c[1] for c in calls], ["create", "cp", "start", "rm"]
        )
        self.assertEqual(calls[-1][-1], "a" * 64)

    def test_validator_cleanup_failure_cannot_be_success(self):
        import subprocess

        import validate_observability as validator

        replies = [
            subprocess.CompletedProcess([], 0, b"b" * 64),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
        ]
        with mock.patch.object(
            validator.subprocess, "run", side_effect=replies
        ):
            result = validator.execute(validator.jobs()[0])
        self.assertEqual(result["status"], "cleanup-failed")
