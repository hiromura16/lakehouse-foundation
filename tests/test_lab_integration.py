"""Check opt-in lab wiring with synthetic settings, never deployment."""

import json
import tempfile
import unittest

import yaml

from tests.test_company_compatibility import (
    COMPOSE_FILES,
    GUI_SETTINGS,
    ROOT,
    SETTINGS,
    PrivateSettingsFixture,
)


class LabSettingsTests(PrivateSettingsFixture, unittest.TestCase):
    """Exercise generator/upgrade paths without network or real secrets."""

    def token_file(self):
        path = self.directory / "synthetic-ingest.json"
        path.write_text(json.dumps({"token": "o2oi_" + "s" * 32}))
        path.chmod(0o600)
        return path

    def test_disabled_labs_do_not_require_credentials_token_file_or_lab_port(
        self,
    ):
        values = self.read_literal()
        for key in (
            "OPENOBSERVE_ROOT_USER_EMAIL",
            "OPENOBSERVE_ROOT_USER_PASSWORD",
        ):
            values.pop(key)
        values["OPENOBSERVE_PORT"] = "unused"
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = "/nonexistent/unused.json"
        self.write_literal(values)
        loaded = SETTINGS.load(self.env_file)
        self.assertEqual(loaded["COMPOSE_PROFILES"], "")
        self.assertEqual(loaded["OPENOBSERVE_PORT"], "unused")

    def test_enabled_openobserve_requires_monitoring_private_token_and_port(
        self,
    ):
        values = self.read_literal()
        values["COMPOSE_PROFILES"] = "openobserve-lab"
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "monitoring"):
            SETTINGS.load(self.env_file)
        values["COMPOSE_PROFILES"] = "monitoring,openobserve-lab"
        self.write_literal(values)
        with self.assertRaises(ValueError):
            SETTINGS.load(self.env_file)
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = str(self.token_file())
        self.write_literal(values)
        self.assertIn(
            "openobserve-lab", SETTINGS.load(self.env_file)["COMPOSE_PROFILES"]
        )
        values["OPENOBSERVE_PORT"] = values["GRAFANA_PORT"]
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "port|Port"):
            SETTINGS.load(self.env_file)

    def test_upgrade_preserves_bootstrap_credentials_and_selected_token_file(
        self,
    ):
        values = self.read_literal()
        values["OPENOBSERVE_ROOT_USER_PASSWORD"] = "synthetic-" + "s" * 40
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = str(self.token_file())
        values.pop("MONITORING_HOST_ALIAS")
        self.write_literal(values)
        self.configure("--upgrade")
        after = self.read_literal()
        self.assertEqual(after["MONITORING_HOST_ALIAS"], "local")
        for key in (
            "OPENOBSERVE_ROOT_USER_PASSWORD",
            "OPENOBSERVE_INGEST_TOKEN_FILE",
            "CATALOG_DB_PASSWORD",
        ):
            self.assertEqual(after[key], values[key])
        self.assertIn(
            "OPENOBSERVE_ROOT_USER_PASSWORD", GUI_SETTINGS.SECRET_KEYS
        )
        self.assertIn("OPENOBSERVE_INGEST_TOKEN_FILE", GUI_SETTINGS.MOUNT_KEYS)

    def test_sail_profile_adds_no_database_or_lab_credentials(self):
        values = self.read_literal()
        values["COMPOSE_PROFILES"] = "sail-lab"
        values.pop("OPENOBSERVE_ROOT_USER_PASSWORD")
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "compute"):
            SETTINGS.load(self.env_file)
        values["COMPOSE_PROFILES"] = "compute,sail-lab"
        self.write_literal(values)
        self.assertEqual(
            SETTINGS.load(self.env_file)["COMPOSE_PROFILES"],
            "compute,sail-lab",
        )

    def test_host_monitoring_requires_separate_explicit_profile(self):
        values = self.read_literal()
        values["COMPOSE_PROFILES"] = "monitoring-host"
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "monitoring"):
            SETTINGS.load(self.env_file)
        values["COMPOSE_PROFILES"] = "monitoring"
        values["CADVISOR_DOCKER_DATA_ROOT"] = "/"
        self.write_literal(values)
        # An inactive exporter path must not gain access or require inspection.
        self.assertEqual(
            SETTINGS.load(self.env_file)["COMPOSE_PROFILES"], "monitoring"
        )
        values["COMPOSE_PROFILES"] = "monitoring,monitoring-host"
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "directory"):
            SETTINGS.load(self.env_file)
        values["CADVISOR_DOCKER_DATA_ROOT"] = ""
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "CADVISOR_DOCKER_DATA_ROOT"):
            SETTINGS.load(self.env_file)
        with tempfile.TemporaryDirectory(prefix="cadvisor-fixture-") as root:
            values["CADVISOR_DOCKER_DATA_ROOT"] = root
            self.write_literal(values)
            self.assertEqual(
                SETTINGS.load(self.env_file)["COMPOSE_PROFILES"],
                "monitoring,monitoring-host",
            )

    def test_gui_rejects_missing_profile_prerequisites_without_saving(self):
        store = GUI_SETTINGS.SettingsStore(self.env_file)
        original = self.env_file.read_bytes()
        for profiles in ("monitoring-host", "sail-lab"):
            with self.subTest(profiles=profiles):
                with self.assertRaises(ValueError):
                    store.save({"COMPOSE_PROFILES": profiles})
                self.assertEqual(self.env_file.read_bytes(), original)

    def test_gui_reads_and_saves_with_only_the_relocated_private_mount(self):
        """Model the container view without creating any actual host path."""
        token = self.token_file()
        values = self.read_literal()
        host_directory = "/synthetic/operator/private"
        host_token = host_directory + "/" + token.name
        values.update(
            COMPOSE_PROFILES="monitoring,openobserve-lab",
            CONTROL_SETTINGS_DIR=host_directory,
            OPENOBSERVE_INGEST_TOKEN_FILE=host_token,
        )
        self.write_literal(values)
        # Host-only validation cannot see the deliberately unmounted path.
        with self.assertRaises(ValueError):
            SETTINGS.load(self.env_file)
        store = GUI_SETTINGS.SettingsStore(self.env_file)
        self.assertEqual(
            store.read()["OPENOBSERVE_INGEST_TOKEN_FILE"], host_token
        )
        public = store.public()
        self.assertEqual(
            public["values"]["OPENOBSERVE_INGEST_TOKEN_FILE"], host_token
        )
        self.assertNotIn(
            "OPENOBSERVE_INGEST_TOKEN_FILE", public["secret_keys"]
        )
        self.assertNotIn(
            "OPENOBSERVE_INGEST_TOKEN_FILE", public["configured_secrets"]
        )
        self.assertNotIn(
            json.loads(token.read_text())["token"], json.dumps(public)
        )
        store.save({"MONITORING_HOST_ALIAS": "lab-alias"})
        saved = self.read_literal()
        self.assertEqual(saved["MONITORING_HOST_ALIAS"], "lab-alias")
        self.assertEqual(saved["OPENOBSERVE_INGEST_TOKEN_FILE"], host_token)
        self.assertEqual(saved["CONTROL_SETTINGS_DIR"], host_directory)
        self.assertNotIn(
            values["OPENOBSERVE_ROOT_USER_PASSWORD"],
            json.dumps(store.public()),
        )
        values = saved
        values["OPENOBSERVE_INGEST_TOKEN_FILE"] = "/unmounted/elsewhere.json"
        self.write_literal(values)
        with self.assertRaises(ValueError):
            store.read()


class LabSourceTests(unittest.TestCase):
    """Verify private endpoints and defaults in the real Compose source."""

    def test_nine_compose_files_and_all_service_bypass_names_are_registered(
        self,
    ):
        self.assertEqual(len(COMPOSE_FILES), 9)
        self.assertEqual(
            [path.parent.name for path in COMPOSE_FILES[-2:]],
            ["openobserve", "sail"],
        )
        names = set()
        for path in COMPOSE_FILES:
            names.update(yaml.safe_load(path.read_text())["services"])
        self.assertEqual(names - set(SETTINGS.REQUIRED_NO_PROXY), set())

    def test_lab_services_publish_only_the_optional_loopback_ui(self):
        for module in ("openobserve", "sail"):
            config = yaml.safe_load(
                (ROOT / "infra" / module / "compose.yaml").read_text()
            )
            for name, service in config["services"].items():
                with self.subTest(service=name):
                    self.assertEqual(service["profiles"], [module + "-lab"])
                    self.assertNotIn("privileged", service)
                    self.assertEqual(service["cap_drop"], ["ALL"])
                    self.assertNotIn("docker.sock", str(service))
                    if module == "sail":
                        self.assertNotIn("ports", service)
                    else:
                        self.assertEqual(
                            service["ports"],
                            ["127.0.0.1:${OPENOBSERVE_PORT:-5080}:5080"],
                        )
                        self.assertFalse(
                            any(
                                key.lower() in {"http_proxy", "https_proxy"}
                                for key in service["environment"]
                            )
                        )

    def test_vector_config_file_does_not_inline_or_interpolate_private_token(
        self,
    ):
        core = yaml.safe_load(
            (ROOT / "infra/lakehouse/compose.yaml").read_text()
        )
        config = core["configs"]["openobserve-vector-token"]
        self.assertNotIn("content", config)
        self.assertIn("OPENOBSERVE_INGEST_TOKEN_FILE", config["file"])
        self.assertNotIn(
            "OPENOBSERVE_ROOT_USER_PASSWORD", str(core["services"]["vector"])
        )
        self.assertNotIn(
            "openobserve", core["services"]["vector"].get("depends_on", {})
        )
        for service in ("prometheus", "grafana", "loki"):
            self.assertNotIn(
                "openobserve", core["services"][service].get("depends_on", {})
            )
