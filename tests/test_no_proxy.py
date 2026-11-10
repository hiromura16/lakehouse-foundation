"""Check canonical private bypass merging without Docker or company data."""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bypass_settings_subject", ROOT / "scripts/settings.py"
)
SETTINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETTINGS)


class CanonicalBypassTests(unittest.TestCase):
    """Preserve private extras and required services without duplicates."""

    def test_merge_preserves_all_private_sources_and_ignores_ambient_values(
        self,
    ):
        values = {
            "LAKEHOUSE_NO_PROXY": " TRINO, first.example.invalid,, ",
            "NO_PROXY": "first.EXAMPLE.invalid,100.64.0.0/10",
            "no_proxy": "second.example.invalid, storage,::1",
        }
        original = dict(values)
        with mock.patch.dict(
            os.environ,
            {
                "NO_PROXY": "ambient.example.invalid",
                "no_proxy": "ambient-lower.example.invalid",
                "LAKEHOUSE_NO_PROXY": "ambient-canonical.example.invalid",
            },
        ):
            result = SETTINGS.normalize_no_proxy(values)
        entries = result.split(",")
        self.assertEqual(
            entries[: len(SETTINGS.REQUIRED_NO_PROXY)],
            list(SETTINGS.REQUIRED_NO_PROXY),
        )
        self.assertEqual(
            entries[-3:],
            [
                "first.example.invalid",
                "100.64.0.0/10",
                "second.example.invalid",
            ],
        )
        self.assertEqual(
            len(entries), len({item.casefold() for item in entries})
        )
        self.assertNotIn("ambient", result)
        self.assertEqual(values, original)

    def test_synchronization_is_idempotent_and_does_not_mutate_input(self):
        original = {"NO_PROXY": "custom.example.invalid", "UNCHANGED": "value"}
        values = SETTINGS.synchronize_no_proxy(original)
        self.assertEqual(
            original,
            {"NO_PROXY": "custom.example.invalid", "UNCHANGED": "value"},
        )
        self.assertEqual(len({values[key] for key in SETTINGS.BYPASS_KEYS}), 1)
        self.assertEqual(SETTINGS.synchronize_no_proxy(values), values)
        self.assertTrue(
            set(SETTINGS.REQUIRED_NO_PROXY)
            <= set(values["NO_PROXY"].split(","))
        )

    def test_subprocess_overrides_all_shell_names_with_private_canonical(self):
        with mock.patch.dict(
            os.environ,
            {key: "stale.example.invalid" for key in SETTINGS.BYPASS_KEYS},
        ):
            result = SETTINGS.environment(
                {"no_proxy": "private.example.invalid"}
            )
        self.assertEqual(len({result[key] for key in SETTINGS.BYPASS_KEYS}), 1)
        self.assertNotIn("stale.example.invalid", result["NO_PROXY"])
        self.assertIn("private.example.invalid", result["NO_PROXY"])

    def test_missing_or_hand_edited_canonical_cannot_remove_required_names(
        self,
    ):
        for values in (
            {},
            {"LAKEHOUSE_NO_PROXY": ""},
            {"LAKEHOUSE_NO_PROXY": "external.example.invalid"},
        ):
            with self.subTest(values=values):
                result = SETTINGS.normalize_no_proxy(values)
                self.assertTrue(
                    set(SETTINGS.REQUIRED_NO_PROXY) <= set(result.split(","))
                )

    def test_required_names_cover_real_compose_and_installer_services(self):
        actual = set()
        for name in (
            "lakehouse",
            "compute",
            "orchestration",
            "demo",
            "cdc",
            "poc",
            "mcp",
        ):
            document = yaml.safe_load(
                (ROOT / "infra" / name / "compose.yaml").read_text()
            )
            actual.update(document["services"])
        extras = {
            "localhost",
            "127.0.0.1",
            "::1",
            "dokploy",
            "dokploy-postgres",
            "dokploy-traefik",
        }
        self.assertEqual(set(SETTINGS.REQUIRED_NO_PROXY), actual | extras)

    def test_invalid_value_types_fail_without_echoing_values(self):
        with self.assertRaises(ValueError):
            SETTINGS.normalize_no_proxy({"NO_PROXY": None})


class ProxyOverrideTests(unittest.TestCase):
    """Exercise the documented override as a real Compose merge."""

    def test_override_runtime_and_build_keep_required_canonical_bypass(self):
        if shutil.which("docker") is None:
            self.skipTest("Static override checks need Docker CLI.")
        version = subprocess.run(
            ["docker", "compose", "version"], capture_output=True
        )
        if version.returncode:
            self.skipTest("Static checks need Compose plugin.")
        example = (ROOT / "infra/proxy/compose.proxy.yaml.example").read_text()
        extension, service = example.split("# services:\n", 1)
        # Activate the documented fields exactly as an operator would copy
        # them. No container, build or network operation is performed.
        override = (
            extension
            + "services:\n"
            + "\n".join(
                line.removeprefix("# ") for line in service.splitlines()
            )
        )
        environment = dict(os.environ)
        for key in SETTINGS.BYPASS_KEYS:
            environment.pop(key, None)
        environment.update(
            NO_PROXY="shell-upper.example.invalid",
            no_proxy="shell-lower.example.invalid",
            COMPOSE_PROFILES="",
        )
        with tempfile.TemporaryDirectory(prefix="proxy-override-") as name:
            directory = Path(name)
            base = directory / "base.yaml"
            base.write_text(
                "services:\n  worker:\n    build:\n      context: .\n"
                "    environment:\n      NO_PROXY: old.example.invalid\n"
                "      no_proxy: old.example.invalid\n"
            )
            addition = directory / "proxy.yaml"
            addition.write_text(override)
            private = directory / "site.env"
            for canonical in (
                "",
                SETTINGS.normalize_no_proxy(
                    {"NO_PROXY": "private.example.invalid"}
                ),
            ):
                with self.subTest(canonical_present=bool(canonical)):
                    private.write_text("LAKEHOUSE_NO_PROXY=" + canonical)
                    private.chmod(0o600)
                    process = subprocess.run(
                        [
                            "docker",
                            "compose",
                            "--env-file",
                            str(private),
                            "-f",
                            str(base),
                            "-f",
                            str(addition),
                            "config",
                            "--format",
                            "json",
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    worker = json.loads(process.stdout)["services"]["worker"]
                    for values in (
                        worker["environment"],
                        worker["build"]["args"],
                    ):
                        self.assertEqual(
                            values["NO_PROXY"], values["no_proxy"]
                        )
                        self.assertTrue(
                            set(SETTINGS.REQUIRED_NO_PROXY)
                            <= set(values["NO_PROXY"].split(","))
                        )
                        if canonical:
                            self.assertEqual(values["NO_PROXY"], canonical)
                            self.assertNotIn("shell-", values["NO_PROXY"])


if __name__ == "__main__":
    unittest.main()
