"""Check private proxy migration and Compose paths without starting Docker."""

import contextlib
import importlib.util
import io
import json
import os
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILES = [
    ROOT / "infra" / name / "compose.yaml"
    for name in (
        "lakehouse",
        "compute",
        "orchestration",
        "demo",
        "cdc",
        "poc",
        "mcp",
    )
]
ALL_PROFILES = (
    "automation,monitoring,compute,orchestration,demo,cdc,bi,notebooks,"
    "vector,lineage,mcp"
)
CORE_SERVICES = {
    "catalog-db",
    "storage",
    "catalog-migrate",
    "catalog",
    "trino",
    "worker",
}


def load_module(name, path):
    """Load the real file so tests exercise CLI and GUI implementations."""
    specification = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


SETTINGS = load_module("company_settings", ROOT / "scripts/settings.py")
GUI_SETTINGS = load_module(
    "company_gui_settings", ROOT / "infra/demo/control_settings.py"
)


class PrivateSettingsFixture:
    """Create realistic private settings without touching a deployed site."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="company-settings-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.env_file = self.directory / "site.env"
        self.configure()

    def configure(self, *arguments):
        """Generate or upgrade a synthetic external private settings file."""
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/configure.py"),
                "--output",
                str(self.env_file),
                *arguments,
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def read_literal(self):
        """Inspect physical assignments separately from loader defaults."""
        return {
            line.partition("=")[0]: line.partition("=")[2]
            for line in self.env_file.read_text().splitlines()
            if line and not line.startswith("#")
        }

    def write_literal(self, values):
        """Write literal synthetic values while retaining private file mode."""
        self.env_file.write_text(
            "\n".join(key + "=" + value for key, value in values.items())
            + "\n"
        )
        self.env_file.chmod(0o600)

    def legacy_values(self):
        """Model an old site with different user additions in each case."""
        values = self.read_literal()
        values.pop("LAKEHOUSE_NO_PROXY", None)
        values["NO_PROXY"] = "localhost,upper.example.invalid"
        values["no_proxy"] = "localhost,lower.example.invalid"
        return values

    def assert_canonical(self, values, extras=()):
        """Check required destinations and both client naming conventions."""
        canonical = values["LAKEHOUSE_NO_PROXY"]
        self.assertEqual(values["NO_PROXY"], canonical)
        self.assertEqual(values["no_proxy"], canonical)
        tokens = canonical.split(",")
        lowered = [token.lower() for token in tokens]
        self.assertEqual(len(lowered), len(set(lowered)))
        self.assertTrue(set(SETTINGS.REQUIRED_NO_PROXY) <= set(tokens))
        self.assertTrue(set(extras) <= set(tokens))
        return tokens


class PrivateSettingsCompatibilityTests(
    PrivateSettingsFixture, unittest.TestCase
):
    """Keep deployment choices and credentials through proxy normalization."""

    def test_fixed_cdc_policy_is_explicit_and_volume_is_a_name(self):
        """Keep generic CDC defaults and reject private path substitution."""
        original = self.read_literal()
        self.assertEqual(original["CDC_SOURCE_MODE"], "generic")
        self.assertEqual(original["CDC_STATE_VOLUME_NAME"], "")
        self.assertEqual(
            original["SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE"], ""
        )
        for key, value in (
            ("CDC_SOURCE_MODE", "automatic"),
            ("CDC_STATE_VOLUME_NAME", "../private"),
            ("CDC_STATE_VOLUME_NAME", "invalid/name"),
        ):
            self.write_literal({**original, key: value})
            with self.assertRaises(ValueError):
                SETTINGS.load(self.env_file)

    def test_fixed_cdc_ca_requires_blank_system_trust_or_private_pem(self):
        """Never interpret the historical 'system' sentinel as a PEM path."""
        values = {
            **self.read_literal(),
            "COMPOSE_PROFILES": "cdc",
            "CDC_SOURCE_MODE": "fixed-demo",
            "SOURCE_SQLSERVER_CA_FILE": "system",
        }
        self.write_literal(values)
        with self.assertRaises(ValueError):
            SETTINGS.load(self.env_file)
        values["SOURCE_SQLSERVER_CA_FILE"] = ""
        values["CDC_STATE_VOLUME_NAME"] = "fixture-fixed-state"
        self.write_literal(values)
        loaded = SETTINGS.load(self.env_file)
        self.assertEqual(
            loaded["CDC_STATE_VOLUME_NAME"], "fixture-fixed-state"
        )

    def test_cdc_upgrade_preserves_existing_settings_and_secrets(self):
        """Adding mode selection cannot rotate existing source credentials."""
        original = self.read_literal()
        original.pop("CDC_SOURCE_MODE")
        original.pop("CDC_STATE_VOLUME_NAME")
        original["SOURCE_SQLSERVER_PASSWORD"] = "fixture-source-secret"
        original["SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE"] = (
            "tls.example.invalid"
        )
        self.write_literal(original)
        self.configure("--upgrade")
        upgraded = self.read_literal()
        self.assertEqual(upgraded["CDC_SOURCE_MODE"], "generic")
        self.assertEqual(
            upgraded["SOURCE_SQLSERVER_PASSWORD"],
            original["SOURCE_SQLSERVER_PASSWORD"],
        )
        self.assertEqual(
            upgraded["SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE"],
            "tls.example.invalid",
        )

    def test_fixed_cdc_custom_pem_needs_independent_java_trust(self):
        """Do not report a Python-only CA path as a complete CDC setup."""
        values = {
            **self.read_literal(),
            "COMPOSE_PROFILES": "cdc",
            "CDC_SOURCE_MODE": "fixed-demo",
            "SOURCE_SQLSERVER_CA_FILE": "/synthetic/source-ca.pem",
            "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "false",
        }
        self.write_literal(values)
        with self.assertRaisesRegex(ValueError, "Java CDC truststore"):
            SETTINGS.load(self.env_file)
        values["CDC_SQLSERVER_TRUSTSTORE"] = "/run/cdc/example.p12"
        self.write_literal(values)
        self.assertEqual(
            SETTINGS.load(self.env_file)["SOURCE_SQLSERVER_CA_FILE"],
            "/synthetic/source-ca.pem",
        )

    def test_fixed_batch_defaults_and_bounded_policy_are_validated(self):
        """Never turn an invalid overlap into an unbounded SQL scan."""
        original = self.read_literal()
        self.assertEqual(
            original["SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS"], "300"
        )
        self.assertEqual(
            original["SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS"], "5"
        )
        self.assertEqual(original["SOURCE_SQLSERVER_CONTROL_PASSWORD"], "")
        for suffix, value in (
            ("OVERLAP_SECONDS", "0"),
            ("OVERLAP_SECONDS", "86401"),
            ("SAFETY_LAG_SECONDS", "-1"),
            ("SAFETY_LAG_SECONDS", "1.5"),
        ):
            values = {**original, "SOURCE_SQLSERVER_BATCH_" + suffix: value}
            self.write_literal(values)
            with self.assertRaises(ValueError):
                SETTINGS.load(self.env_file)

    def test_batch_upgrade_preserves_control_and_ownership_credentials(self):
        """Adding overlap policy must not rotate an installed ownership key."""
        original = self.read_literal()
        original.pop("SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS")
        original.pop("SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS")
        original["SOURCE_SQLSERVER_CONTROL_PASSWORD"] = "synthetic-control"
        self.write_literal(original)
        self.configure("--upgrade")
        upgraded = self.read_literal()
        self.assertEqual(
            upgraded["SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS"], "300"
        )
        for key in (
            "SOURCE_SQLSERVER_CONTROL_PASSWORD",
            "DEMO_SQLSERVER_OWNERSHIP_TOKEN",
            "DEMO_SQLSERVER_INSTALLATION_ID",
            "S3_SECRET_KEY",
        ):
            self.assertEqual(upgraded[key], original[key])

    def test_buildkit_default_is_generated_and_not_a_secret(self):
        """Persist a strict private switch without treating it as a token."""
        values = self.read_literal()
        self.assertEqual(values["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"], "1")
        public = GUI_SETTINGS.SettingsStore(self.env_file).public()
        self.assertEqual(
            public["values"]["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"], "1"
        )
        self.assertNotIn(
            "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN", public["secret_keys"]
        )

    def test_upgrade_adds_buildkit_switch_without_rotating_existing_secrets(
        self,
    ):
        """Upgrade an old site's missing switch while keeping every value."""
        before = self.read_literal()
        before.pop("DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN")
        before.update(
            SOURCE_SQLSERVER_PASSWORD="synthetic-source-secret",
            DEMO_SQLSERVER_PASSWORD="synthetic-demo-secret",
            CUSTOM_PRESERVED_VALUE="synthetic-custom-choice",
            HTTP_PROXY=(
                "http://example:synthetic-secret@proxy.example.invalid:3128"
            ),
        )
        self.write_literal(before)
        original = self.env_file.read_bytes()
        result = self.configure("--upgrade")
        after = self.read_literal()
        self.assertEqual(after["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"], "1")
        for key, value in before.items():
            self.assertEqual(after[key], value, "Changed key: " + key)
        self.assertEqual(
            self.env_file.with_name("site.env.before-upgrade").read_bytes(),
            original,
        )
        self.assertNotIn("synthetic-secret", result.stdout + result.stderr)
        saved = self.env_file.read_bytes()
        self.configure("--upgrade")
        self.assertEqual(self.env_file.read_bytes(), saved)

    def test_upgrade_preserves_explicit_buildkit_disable(self):
        """Adding later keys must not re-enable an operator's zero setting."""
        before = self.read_literal()
        before["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"] = "0"
        self.write_literal(before)
        self.configure("--upgrade")
        self.assertEqual(
            self.read_literal()["DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN"], "0"
        )

    def test_plan_loader_rejects_invalid_buildkit_flag_before_docker(self):
        """The normal CLI loader rejects ambiguous boolean-like strings."""
        before = self.read_literal()
        for value in ("", "true", "yes please", "2"):
            with self.subTest(value=value):
                self.write_literal(
                    {**before, "DOKPLOY_BUILDKIT_NO_CLIENT_TOKEN": value}
                )
                with self.assertRaisesRegex(ValueError, "must be 0 or 1"):
                    SETTINGS.load(self.env_file)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/control.py"),
                        "--env-file",
                        str(self.env_file),
                        "plan",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("must be 0 or 1", result.stderr)
                self.assertNotIn("yes please", result.stderr)

    def test_cli_ignores_stale_shell_bypass_and_merges_private_variants(self):
        """The selected file determines CLI routing, including legacy sites."""
        self.write_literal(self.legacy_values())
        with (
            mock.patch.dict(
                os.environ,
                {
                    "NO_PROXY": "shell-upper.example.invalid",
                    "no_proxy": "shell-lower.example.invalid",
                    "LAKEHOUSE_NO_PROXY": "shell-canonical.example.invalid",
                },
            ),
            mock.patch.dict(sys.modules, {"settings": SETTINGS}),
            mock.patch.object(
                sys,
                "argv",
                [
                    "control.py",
                    "--env-file",
                    str(self.env_file),
                    "plan",
                ],
            ),
            mock.patch("subprocess.run") as process,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(
                str(ROOT / "scripts/control.py"), run_name="__main__"
            )
        process.assert_called_once()
        selected = process.call_args.kwargs["env"]
        tokens = self.assert_canonical(
            selected, ("upper.example.invalid", "lower.example.invalid")
        )
        self.assertFalse(any(token.startswith("shell-") for token in tokens))
        self.assertEqual(selected["COMPOSE_PROFILES"], "")

    def test_upgrade_saves_legacy_additions_without_rotating_keys(self):
        """Persist the canonical key for direct Dokploy Compose rendering."""
        before = self.legacy_values()
        self.write_literal(before)
        original = self.env_file.read_bytes()
        self.configure("--upgrade")
        after = self.read_literal()
        self.assert_canonical(
            after, ("upper.example.invalid", "lower.example.invalid")
        )
        for key in before.keys() - {"NO_PROXY", "no_proxy"}:
            self.assertEqual(after[key], before[key], "Changed key: " + key)
        backup = self.env_file.with_name("site.env.before-upgrade")
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        saved = self.env_file.read_bytes()
        self.configure("--upgrade")
        self.assertEqual(self.env_file.read_bytes(), saved)
        self.assertEqual(backup.read_bytes(), original)

    def test_upgrade_refuses_backup_collision_without_touching_files(self):
        """Explain an occupied backup while retaining both private files."""
        self.write_literal(self.legacy_values())
        original = self.env_file.read_bytes()
        backup = self.env_file.with_name("site.env.before-upgrade")
        saved_backup = b"previous private backup; keep these bytes\n"
        backup.write_bytes(saved_backup)
        backup.chmod(0o600)
        files_before = set(self.directory.iterdir())

        with self.assertRaises(subprocess.CalledProcessError) as failure:
            self.configure("--upgrade")

        result = failure.exception
        self.assertEqual(result.returncode, 1)
        self.assertIn("backup already exists", result.stderr)
        self.assertIn("Move the .before-upgrade backup", result.stderr)
        self.assertIn("rerun --upgrade", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("FileExistsError", result.stderr)
        self.assertNotIn(str(self.directory), result.stderr)
        self.assertNotIn(self.read_literal()["WORKER_TOKEN"], result.stderr)
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), saved_backup)
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(self.directory.iterdir()), files_before)

    def test_upgrade_noop_preserves_an_existing_backup(self):
        """A current env needs no new backup and still exits successfully."""
        original = self.env_file.read_bytes()
        backup = self.env_file.with_name("site.env.before-upgrade")
        saved_backup = b"retained backup from an earlier upgrade\n"
        backup.write_bytes(saved_backup)
        backup.chmod(0o600)

        result = self.configure("--upgrade")

        self.assertIn("no changes made", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(self.env_file.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), saved_backup)

    def test_missing_external_env_has_a_distinct_private_error(self):
        """Report a missing external file without a traceback or its path."""
        missing = self.directory / "missing.env"
        with self.assertRaisesRegex(
            ValueError, r"^Private env file not found\.$"
        ):
            SETTINGS.load(missing)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/control.py"),
                "--env-file",
                str(missing),
                "plan",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "Private env file not found.")
        self.assertEqual(result.stdout, "")
        self.assertFalse(missing.exists())

    def test_repository_env_paths_remain_rejected_before_file_lookup(self):
        """Both existing and missing Git-local paths keep the policy error."""
        for path in (
            ROOT / "scripts/settings.py",
            ROOT / "missing-private-settings.env",
        ):
            with self.subTest(existing=path.is_file()):
                with self.assertRaisesRegex(
                    ValueError,
                    r"^Use a private env file outside the repository\.$",
                ):
                    SETTINGS.load(path)

    def test_gui_case_edits_preserve_removal_intent_and_required_hosts(self):
        """Editing one field replaces custom additions in all three fields."""
        original = self.read_literal()
        for edited in ("NO_PROXY", "no_proxy", "LAKEHOUSE_NO_PROXY"):
            with self.subTest(edited=edited):
                self.write_literal(original)
                store = GUI_SETTINGS.SettingsStore(self.env_file)
                store.save({edited: "old.example.invalid"})
                saved = store.save({edited: "new.example.invalid"})
                values = self.read_literal()
                tokens = self.assert_canonical(
                    values, ("new.example.invalid",)
                )
                self.assertNotIn("old.example.invalid", tokens)
                self.assertFalse(saved["applied"])
                self.assertEqual(values["COMPOSE_PROFILES"], "")
                self.assertEqual(
                    values["WORKER_TOKEN"], original["WORKER_TOKEN"]
                )
                store.save({edited: ""})
                self.assertEqual(
                    self.read_literal()["LAKEHOUSE_NO_PROXY"],
                    ",".join(SETTINGS.REQUIRED_NO_PROXY),
                )


class ComposeCompatibilityTests(PrivateSettingsFixture, unittest.TestCase):
    """Render real Compose/Bake plans without a daemon or network calls."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("docker") is None:
            raise unittest.SkipTest("Static Compose checks need Docker CLI.")
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise unittest.SkipTest("Static checks need Compose plugin.")

    def compose(self, *, profiles="", legacy=False, cwd=None, files=None):
        """Render all files with explicit profiles and stale ambient values."""
        values = self.read_literal()
        values["COMPOSE_PROFILES"] = profiles
        if legacy:
            values.pop("LAKEHOUSE_NO_PROXY", None)
        self.write_literal(values)
        environment = dict(os.environ)
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "LAKEHOUSE_NO_PROXY",
        ):
            environment.pop(key, None)
        environment.update(
            NO_PROXY="shell-upper.example.invalid",
            no_proxy="shell-lower.example.invalid",
            COMPOSE_PROFILES=profiles,
        )
        command = ["docker", "compose", "--env-file", str(self.env_file)]
        for path in files or COMPOSE_FILES:
            command.extend(["-f", str(path)])
        result = subprocess.run(
            command + ["config", "--format", "json"],
            cwd=cwd or ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def assert_service_bypass(self, config, expected=None):
        """Verify every declared proxy consumer uses the same bypass value."""
        consumers = 0
        for name, service in config["services"].items():
            sections = [service.get("environment", {})]
            sections.append(service.get("build", {}).get("args", {}))
            for section in sections:
                if "NO_PROXY" not in section and "no_proxy" not in section:
                    continue
                with self.subTest(service=name):
                    consumers += 1
                    self.assertEqual(section["NO_PROXY"], section["no_proxy"])
                    tokens = section["NO_PROXY"].split(",")
                    self.assertTrue(
                        set(SETTINGS.REQUIRED_NO_PROXY) <= set(tokens)
                    )
                    if expected is not None:
                        self.assertEqual(section["NO_PROXY"], expected)
        self.assertGreater(consumers, 0)

    def test_raw_dokploy_uses_upgraded_key_despite_stale_shell(self):
        """Dedicated settings bypass Compose's legacy shell-key collision."""
        self.write_literal(self.legacy_values())
        self.configure("--upgrade")
        expected = self.read_literal()["LAKEHOUSE_NO_PROXY"]
        config = self.compose(profiles=ALL_PROFILES)
        self.assert_service_bypass(config, expected)
        self.assertTrue(
            set(config["services"]) <= set(SETTINGS.REQUIRED_NO_PROXY)
        )
        for name in ("worker", "trino", "n8n", "jupyter", "demo-api"):
            self.assertIn("NO_PROXY", config["services"][name]["environment"])

    def test_unupgraded_raw_compose_still_bypasses_internal_hosts(self):
        """Fallback protects service routing before private-file migration."""
        self.assert_service_bypass(self.compose(legacy=True))

    def test_empty_profiles_keep_six_core_services(self):
        """Generated optional secrets and ports never activate profiles."""
        for files in ((COMPOSE_FILES[0],), COMPOSE_FILES):
            with self.subTest(file_count=len(files)):
                config = self.compose(files=files)
                self.assertEqual(set(config["services"]), CORE_SERVICES)

    def test_empty_ca_resolves_inside_checkout_from_any_working_directory(
        self,
    ):
        """Compose resolves secret and n8n mount from the first file."""
        expected = ROOT / "infra/proxy/empty-ca.pem"
        self.assertTrue(expected.is_file())
        self.assertEqual(expected.stat().st_size, 0)
        for cwd in (ROOT, self.directory):
            with self.subTest(cwd_kind="repo" if cwd == ROOT else "external"):
                config = self.compose(profiles="automation", cwd=cwd)
                self.assertEqual(
                    Path(config["secrets"]["corporate_ca"]["file"]), expected
                )
                n8n = config["services"]["n8n"]
                mounts = [
                    mount
                    for mount in n8n["volumes"]
                    if mount["target"] == "/etc/n8n/corporate-ca.pem"
                ]
                self.assertEqual(len(mounts), 1)
                self.assertEqual(Path(mounts[0]["source"]), expected)
                self.assertEqual(n8n["environment"]["NODE_EXTRA_CA_CERTS"], "")

    def test_explicit_ca_keeps_external_path_and_node_certificate_setting(
        self,
    ):
        """An operator-selected bundle still overrides the empty default."""
        certificate = self.directory / "corporate-ca.crt"
        # This test renders paths only; no TLS or build reads the contents.
        certificate.write_text("Synthetic public fixture for path checks.\n")
        values = self.read_literal()
        values["CORPORATE_CA_FILE"] = str(certificate)
        self.write_literal(values)
        config = self.compose(profiles="automation")
        self.assertEqual(
            Path(config["secrets"]["corporate_ca"]["file"]), certificate
        )
        n8n = config["services"]["n8n"]
        self.assertEqual(
            n8n["environment"]["NODE_EXTRA_CA_CERTS"],
            "/etc/n8n/corporate-ca.pem",
        )
        mounted = [
            item["source"]
            for item in n8n["volumes"]
            if item["target"] == "/etc/n8n/corporate-ca.pem"
        ]
        self.assertEqual(mounted, [str(certificate)])

    def test_compose_resolved_bake_plan_keeps_ca_inside_build_context(self):
        """Inspect Bake's build inputs without building or daemon access."""
        available = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
            check=False,
        )
        if available.returncode:
            self.skipTest("Bake plan check needs the buildx plugin.")
        config = self.compose(profiles=ALL_PROFILES)
        selected = self.directory / "resolved.compose.yaml"
        selected.write_text(json.dumps(config))
        selected.chmod(0o600)
        result = subprocess.run(
            [
                "docker",
                "buildx",
                "bake",
                "-f",
                str(selected),
                "--print",
            ],
            cwd=self.directory,
            capture_output=True,
            text=True,
            check=True,
        )
        targets = json.loads(result.stdout)["target"]
        self.assertIn("worker", targets)
        for name, target in targets.items():
            with self.subTest(target=name):
                context = Path(target["context"])
                self.assertEqual(context, ROOT)
                if name == "vector":
                    # Vector derives a completed binary using only local
                    # config COPY and mkdir/chown; no HTTPS RUN fetches.
                    self.assertFalse(target.get("secret"))
                    continue
                ca_secrets = [
                    item
                    for item in target.get("secret", [])
                    if item["id"] == "corporate_ca"
                ]
                self.assertEqual(len(ca_secrets), 1)
                source = Path(ca_secrets[0]["src"])
                self.assertTrue(source.is_relative_to(context))
                self.assertEqual(source.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
