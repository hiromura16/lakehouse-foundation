"""公開Portalのリンクと内部health経路を、合成設定だけで検証する。"""

import contextlib
import io
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra/demo"
with mock.patch.object(sys, "path", [str(DEMO), *sys.path]):
    import control_settings
    import poc_services
    from poc_catalog import SERVICES

SETTINGS = control_settings.VALIDATOR


class PublicOriginTests(unittest.TestCase):
    """公開先はHTTPS originだけを許可し、検査時の外部接続を不要にする。"""

    def test_mapping_covers_every_card_and_portal(self):
        expected = {
            item.id: item.port_key.removesuffix("_PORT") + "_PUBLIC_URL"
            for item in SERVICES
        }
        expected["portal"] = "DEMO_GUI_PUBLIC_URL"
        self.assertEqual(SETTINGS.PUBLIC_URL_KEYS, expected)
        self.assertEqual(len(expected), 18)
        self.assertTrue(
            set(expected.values()) <= control_settings.SETTING_KEYS
        )

    def test_origin_accepts_root_slash_case_and_explicit_port(self):
        for value, expected in (
            ("", ""),
            ("https://grafana.example.com/", "https://grafana.example.com"),
            ("https://GRAFANA.example.com", "https://grafana.example.com"),
            (
                "https://grafana.example.com:8443/",
                "https://grafana.example.com:8443",
            ),
        ):
            with self.subTest(value=value):
                self.assertEqual(SETTINGS.public_origin(value), expected)

    def test_unsafe_origins_are_rejected_without_reflecting_values(self):
        rejected = (
            "http://grafana.example.com",
            "//grafana.example.com",
            "https://user:synthetic-pass@grafana.example.com",
            "https://@grafana.example.com",
            "https://grafana.example.com/?token=synthetic",
            "https://grafana.example.com?",
            "https://grafana.example.com/#fragment",
            "https://grafana.example.com#",
            "https://grafana.example.com/dashboards",
            "https://127.0.0.1",
            # Reserved documentation address; keep concrete IPs out of source.
            "https://" + ".".join(map(str, (192, 0, 2, 10))),
            "https://[::1]",
            "https://127.1",
            "https://0x7f.0x0.0x0.0x1",
            "https://localhost",
            "https://grafana.localhost",
            "https://grafana.local",
            "https://grafana.internal",
            "https://grafana.lan",
            "https://grafana",
            "https://-grafana.example.com",
            "https://grafana..example.com",
            "https://grafana_example.com",
            "https://grafana.example.com.",
            "https://grafana.example.com:",
            "https://grafana.example.com:0",
            "https://grafana.example.com:65536",
            "https://grafana.example.com:bad",
            "https://grafana.example.com\\@example.org",
            " https://grafana.example.com",
            "https://grafana.example.com\n",
            "https://grafana.example.com/\x00",
        )
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    SETTINGS.public_origin(value)
                self.assertNotIn(value, str(caught.exception))
                self.assertNotIn("synthetic-pass", str(caught.exception))

    def test_mode_is_strict_and_old_settings_gain_safe_defaults(self):
        values = {}
        SETTINGS.validate_portal_links(values)
        self.assertEqual(values["PORTAL_LINK_MODE"], "local")
        self.assertTrue(
            all(values[key] == "" for key in SETTINGS.PUBLIC_URL_KEYS.values())
        )
        for mode in ("PUBLIC", "", "yes please"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                SETTINGS.validate_portal_links({"PORTAL_LINK_MODE": mode})

    def test_ui_mode_defaults_to_legacy_and_accepts_only_named_modes(self):
        values = {}
        SETTINGS.validate_portal_links(values)
        self.assertEqual(values["PORTAL_UI_MODE"], "streamlit")
        SETTINGS.validate_portal_links({"PORTAL_UI_MODE": "react"})
        for mode in ("REACT", "", "custom-server"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                SETTINGS.validate_portal_links({"PORTAL_UI_MODE": mode})

    def test_public_react_requires_its_origin_before_deploy_plan(self):
        with self.assertRaises(ValueError):
            SETTINGS.validate_portal_links(
                {"PORTAL_UI_MODE": "react", "PORTAL_LINK_MODE": "public"}
            )
        values = {
            "PORTAL_UI_MODE": "react",
            "PORTAL_LINK_MODE": "public",
            "DEMO_GUI_PUBLIC_URL": "https://portal.example.com/",
        }
        SETTINGS.validate_portal_links(values)
        self.assertEqual(
            values["DEMO_GUI_PUBLIC_URL"], "https://portal.example.com"
        )


class PublicCardTests(unittest.TestCase):
    """リンクだけを変更し、到達性判定を公開URLへ向けない。"""

    def test_public_mode_joins_only_fixed_path_and_never_falls_back(self):
        values = {
            "PORTAL_LINK_MODE": "public",
            "COMPOSE_PROFILES": "demo,monitoring,notebooks",
            "GRAFANA_PUBLIC_URL": "https://grafana.example.com/",
            "DEMO_API_PUBLIC_URL": "https://api.example.com",
            "JUPYTER_PUBLIC_URL": "https://notebook.example.com",
        }
        cards = {
            item["id"]: item for item in poc_services.service_cards(values, {})
        }
        self.assertEqual(
            cards["grafana"]["url"], "https://grafana.example.com/"
        )
        self.assertEqual(
            cards["swagger"]["url"], "https://api.example.com/docs"
        )
        self.assertEqual(
            cards["jupyter"]["url"], "https://notebook.example.com/lab"
        )
        self.assertEqual(cards["trino"]["url"], "")
        self.assertFalse(cards["trino"]["link_configured"])
        self.assertTrue(cards["grafana"]["link_configured"])
        self.assertTrue(
            all(card["link_mode"] == "public" for card in cards.values())
        )
        self.assertFalse(
            any("localhost" in card["url"] for card in cards.values())
        )

    def test_local_mode_preserves_all_existing_urls(self):
        values = {
            "COMPOSE_PROFILES": ",".join(
                sorted(
                    {profile for item in SERVICES for profile in item.profiles}
                )
            )
        }
        for mode in (None, "local"):
            if mode:
                values["PORTAL_LINK_MODE"] = mode
            cards = poc_services.service_cards(values, {})
            for item, card in zip(SERVICES, cards, strict=True):
                self.assertEqual(
                    card["url"], f"http://localhost:{item.port}{item.path}"
                )
                self.assertEqual(card["link_mode"], "local")
                self.assertTrue(card["link_configured"])

    def test_api_card_accepts_legacy_payload_with_compatible_defaults(self):
        with mock.patch.object(sys, "path", [str(DEMO), *sys.path]):
            from poc_api import ServiceCard

        card = poc_services.service_cards({}, {})[0]
        card.pop("link_mode")
        card.pop("link_configured")
        parsed = ServiceCard.model_validate(card)
        self.assertEqual(parsed.link_mode, "local")
        self.assertTrue(parsed.link_configured)

    def test_public_url_never_changes_internal_health_destination(self):
        values = {
            "PORTAL_LINK_MODE": "public",
            "COMPOSE_PROFILES": ",".join(
                sorted(
                    {profile for item in SERVICES for profile in item.profiles}
                )
            ),
        }
        for item in SERVICES:
            values[SETTINGS.PUBLIC_URL_KEYS[item.id]] = (
                "https://" + item.id + ".example.com"
            )
            for key in item.required:
                values[key] = "synthetic-value"
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.get.return_value = SimpleNamespace(status_code=200)
        with mock.patch.object(
            poc_services.httpx, "Client", return_value=client
        ):
            result = poc_services.probe_services(values)
        self.assertEqual(set(result), {item.id for item in SERVICES})
        self.assertEqual(
            {call.args[0] for call in client.get.call_args_list},
            {item.health_url for item in SERVICES},
        )
        self.assertTrue(
            all(
                call.args[0].startswith("http://")
                for call in client.get.call_args_list
            )
        )


class PublicSettingsTests(unittest.TestCase):
    """新規・upgrade・GUI保存を実サービスなしの一時設定で確認する。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-portal-unit-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "private/site.env"
        self.configure()

    def configure(self, *arguments):
        with (
            mock.patch.dict(sys.modules, {"settings": SETTINGS}),
            mock.patch.object(
                sys,
                "argv",
                ["configure.py", "--output", str(self.path), *arguments],
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            runpy.run_path(
                str(ROOT / "scripts/configure.py"), run_name="__main__"
            )

    def test_fresh_configuration_contains_every_blank_origin(self):
        values = SETTINGS.load(self.path)
        self.assertEqual(values["PORTAL_LINK_MODE"], "local")
        for key in SETTINGS.PUBLIC_URL_KEYS.values():
            self.assertIn(key + "=\n", self.path.read_text())
        self.assertEqual(values["PORTAL_UI_MODE"], "streamlit")

    def test_upgrade_adds_keys_without_rotating_secrets(self):
        original = SETTINGS.load(self.path)
        omitted = {"PORTAL_LINK_MODE", *SETTINGS.PUBLIC_URL_KEYS.values()}
        content = (
            "\n".join(
                line
                for line in self.path.read_text().splitlines()
                if line.partition("=")[0] not in omitted
            )
            + "\n"
        )
        self.path.write_text(content)
        self.configure("--upgrade")
        upgraded = SETTINGS.load(self.path)
        self.assertEqual(upgraded["WORKER_TOKEN"], original["WORKER_TOKEN"])
        self.assertEqual(upgraded["S3_SECRET_KEY"], original["S3_SECRET_KEY"])
        self.assertEqual(upgraded["PORTAL_LINK_MODE"], "local")
        self.assertEqual(
            self.path.with_name("site.env.before-upgrade").read_text(), content
        )

    def test_upgrade_preserves_previously_selected_public_links(self):
        content = (
            self.path.read_text()
            .replace("PORTAL_LINK_MODE=local\n", "PORTAL_LINK_MODE=public\n")
            .replace(
                "GRAFANA_PUBLIC_URL=\n",
                "GRAFANA_PUBLIC_URL=https://grafana.example.com\n",
            )
            .replace("MCP_PUBLIC_URL=\n", "")
        )
        self.path.write_text(content)
        before = SETTINGS.load(self.path)
        self.configure("--upgrade")
        after = SETTINGS.load(self.path)
        self.assertEqual(after["PORTAL_LINK_MODE"], "public")
        self.assertEqual(
            after["GRAFANA_PUBLIC_URL"], "https://grafana.example.com"
        )
        self.assertEqual(before["WORKER_TOKEN"], after["WORKER_TOKEN"])
        self.assertEqual(after["MCP_PUBLIC_URL"], "")

    def test_saved_links_remain_pending_until_snapshot_is_replaced(self):
        store = control_settings.SettingsStore(self.path)
        result = store.save(
            {
                "PORTAL_LINK_MODE": "public",
                "GRAFANA_PUBLIC_URL": "https://grafana.example.com/",
            }
        )
        self.assertTrue(result["pending_restart"])
        self.assertFalse(result["applied"])
        self.assertEqual(store.applied["PORTAL_LINK_MODE"], "local")
        self.assertEqual(
            store.read()["GRAFANA_PUBLIC_URL"], "https://grafana.example.com"
        )
        self.assertEqual(
            store.applied["WORKER_TOKEN"], store.read()["WORKER_TOKEN"]
        )

    def test_load_rejects_bad_origin_and_mode_without_echo(self):
        with self.path.open("a") as output:
            output.write("UNRELATED_NOTE=synthetic\n")
        original = self.path.read_text()
        for key, value in (
            ("GRAFANA_PUBLIC_URL", "http://private.example.com"),
            ("PORTAL_LINK_MODE", "not-a-mode"),
        ):
            edited = original.replace(
                key + "=local\n", key + "=" + value + "\n"
            )
            if edited == original:
                edited = original.replace(
                    key + "=\n", key + "=" + value + "\n"
                )
            self.path.write_text(edited)
            with self.assertRaises(ValueError) as caught:
                SETTINGS.load(self.path)
            self.assertNotIn(value, str(caught.exception))
        self.path.write_text(original)
