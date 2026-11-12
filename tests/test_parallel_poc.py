"""Exercise fresh parallel-PoC settings without Docker, DB or host apply."""

import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from settings import REQUIRED_NO_PROXY, load  # noqa: E402


class ParallelPocTests(unittest.TestCase):
    """A preset separates defaults; it never verifies actual free ports."""

    def setUp(self):
        """Create only an owned synthetic test directory for each case."""
        temporary = tempfile.TemporaryDirectory(prefix="parallel-poc-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.output = self.directory / "private/site.env"
        self.data = self.directory / "new-data"

    def invoke(
        self, *, data=None, project="onboarding-lab", extra=(), output=None
    ):
        """Invoke generation with captured output and a bounded deadline."""
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(self.output if output is None else output),
                "--preset",
                "parallel-lab",
                "--data-root",
                str(self.data if data is None else data),
                "--project-name",
                project,
                *extra,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    def test_new_settings_do_not_create_data_or_read_another_site(self):
        legacy = self.directory / "existing-main.env"
        legacy.write_text("synthetic existing settings, not valid input\n")
        before = legacy.read_bytes()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = load(self.output)
        self.assertEqual(values["COMPOSE_PROJECT_NAME"], "onboarding-lab")
        self.assertEqual(values["DATA_VOLUME_PREFIX"], "")
        self.assertEqual(values["CDC_STATE_VOLUME_NAME"], "")
        self.assertEqual(values["BIND_IP"], "127.0.0.1")
        self.assertEqual(
            values["COMPOSE_PROFILES"], "demo,monitoring,bi,automation"
        )
        self.assertEqual(values["PORTAL_UI_MODE"], "react")
        self.assertEqual(values["LANDING_DIR"], str(self.data / "landing"))
        self.assertEqual(values["NOTEBOOKS_DIR"], str(self.data / "notebooks"))
        self.assertEqual(
            values["MONITORING_LOG_DIR"], str(self.data / "log-spool")
        )
        self.assertEqual(legacy.read_bytes(), before)
        self.assertFalse(self.data.exists())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(self.output.parent.stat().st_mode), 0o700
        )
        self.assertEqual(
            {item.name for item in self.output.parent.iterdir()}, {"site.env"}
        )
        self.assertEqual(
            values["CONTROL_SETTINGS_DIR"], str(self.output.parent)
        )
        self.assertTrue(
            set(REQUIRED_NO_PROXY) <= set(values["NO_PROXY"].split(","))
        )

    def test_exact_application_ports_shift_but_sql_and_dokploy_do_not(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = load(self.output)
        expected = {
            "TRINO_PORT": 28080,
            "CATALOG_PORT": 28181,
            "S3_PORT": 28333,
            "GRAFANA_PORT": 23000,
            "PROMETHEUS_PORT": 29090,
            "N8N_PORT": 25678,
            "AIRFLOW_PORT": 28081,
            "SPARK_MASTER_UI_PORT": 28082,
            "SPARK_WORKER_UI_PORT": 28085,
            "SPARK_HISTORY_UI_PORT": 38080,
            "DEMO_GUI_PORT": 28501,
            "DEMO_API_PORT": 28502,
            "CDC_CONNECT_PORT": 28083,
            "CDC_UI_PORT": 28084,
            "SUPERSET_PORT": 28088,
            "JUPYTER_PORT": 28888,
            "QDRANT_PORT": 26333,
            "MARQUEZ_API_PORT": 25000,
            "MARQUEZ_UI_PORT": 23001,
            "MCP_PORT": 28503,
            "OPENOBSERVE_PORT": 25080,
        }
        for key, value in expected.items():
            self.assertEqual(values[key], str(value), key)
        self.assertEqual(len(set(expected.values())), len(expected))
        for prefix in ("SOURCE", "DEMO", "CDC"):
            self.assertEqual(values[prefix + "_SQLSERVER_PORT"], "1433")
        self.assertEqual(values["DOKPLOY_ADMIN_PORT"], "3100")

    def test_source_and_labs_are_gated_and_secrets_omitted(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        values = load(self.output)
        for prefix in ("SOURCE", "DEMO", "CDC"):
            for suffix in ("HOST", "DATABASE", "USER", "PASSWORD"):
                self.assertEqual(values[prefix + "_SQLSERVER_" + suffix], "")
        self.assertEqual(values["OPENOBSERVE_INGEST_TOKEN_FILE"], "")
        self.assertEqual(values["CADVISOR_DOCKER_DATA_ROOT"], "")
        self.assertEqual(values["HTTP_PROXY"], "")
        self.assertEqual(values["HTTPS_PROXY"], "")
        self.assertEqual(values["CORPORATE_CA_FILE"], "")
        self.assertEqual(values["MCP_ENABLE_MUTATIONS"], "false")
        for key in ("WORKER_TOKEN", "S3_SECRET_KEY", "JUPYTER_TOKEN"):
            self.assertNotIn(values[key], result.stdout + result.stderr)
        self.assertNotEqual(values["WORKER_TOKEN"], values["S3_SECRET_KEY"])

    def test_existing_file_or_data_is_never_replaced(self):
        self.assertEqual(self.invoke().returncode, 0)
        before = self.output.read_bytes()
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertFalse(self.data.exists())
        self.output.unlink()
        self.data.mkdir(mode=0o700)
        sentinel = self.data / "owned-notebook.txt"
        sentinel.write_bytes(b"unchanged synthetic notebook")
        before_mode = self.data.stat().st_mode
        result = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.data.stat().st_mode, before_mode)
        self.assertEqual(
            sentinel.read_bytes(), b"unchanged synthetic notebook"
        )

    def test_upgrade_and_unused_options_fail_before_creating_settings(self):
        self.assertNotEqual(self.invoke(extra=("--upgrade",)).returncode, 0)
        self.assertFalse(self.output.parent.exists())
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(self.output),
                "--project-name",
                "onboarding-lab",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.parent.exists())

    def test_unsafe_targets_and_project_names_are_refused(self):
        for value in (
            Path("relative/data"),
            Path("/"),
            Path("/etc/new-lab"),
            ROOT / "new-lab",
            self.output.parent / "data",
            self.directory / "ancestor/../new-lab",
            self.directory / "newline\ninjected",
            self.directory / "has space",
            self.directory / ".ssh/new-lab",
        ):
            with self.subTest(target_kind=value.name):
                self.assertNotEqual(self.invoke(data=value).returncode, 0)
                self.assertFalse(self.output.parent.exists())
        for project in ("lakehouse", "UPPER", "bad/project", "bad\nproject"):
            with self.subTest(project_length=len(project)):
                self.assertNotEqual(self.invoke(project=project).returncode, 0)
                self.assertFalse(self.output.parent.exists())

    def test_leaf_ancestor_and_dangling_symlinks_are_refused(self):
        real = self.directory / "real"
        real.mkdir()
        link = self.directory / "linked"
        link.symlink_to(real, target_is_directory=True)
        dangling = self.directory / "dangling"
        dangling.symlink_to(self.directory / "missing")
        for selected in (link, link / "new", dangling, dangling / "new"):
            with self.subTest(target_kind=selected.name):
                self.assertNotEqual(self.invoke(data=selected).returncode, 0)
                self.assertFalse(self.output.parent.exists())
        self.assertEqual(list(real.iterdir()), [])

    def test_output_traversal_cannot_create_data_or_hide_settings_in_it(self):
        disguised = self.directory / "config/../new-data/private/site.env"
        result = self.invoke(output=disguised)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.data.exists())
        self.assertFalse((self.directory / "config").exists())
        self.assertFalse(self.output.parent.exists())

    def test_double_root_aliases_cannot_hide_settings_inside_data(self):
        nested_output = self.data / "private/site.env"
        for output, data in (
            (Path("/" + str(nested_output)), self.data),
            (nested_output, Path("/" + str(self.data))),
        ):
            with self.subTest(double_root=output.anchor == "//"):
                result = self.invoke(data=data, output=output)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.data.exists())
                self.assertFalse(self.output.parent.exists())


if __name__ == "__main__":
    unittest.main()
