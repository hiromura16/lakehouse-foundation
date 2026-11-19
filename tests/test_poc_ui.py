"""Use real portal widgets while keeping all transport local and mocked."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path

# Only a genuinely absent optional package skips this test module. A broken
# installed package or any application import failure must remain an error.
try:
    from streamlit.testing.v1 import AppTest
except ModuleNotFoundError as error:
    if (
        error.name != "streamlit"
        or importlib.util.find_spec("streamlit") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional tests need streamlit. In a uv virtual environment, run "
        "uv pip sync --require-hashes requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]


class PortalUiTests(unittest.TestCase):
    """Confirm portal reads and explicit checks never run a pipeline."""

    def setUp(self):
        self.calls = []
        self.service_override = None
        self.settings_values = {}
        self.settings_pending_restart = False
        client = types.ModuleType("client")
        client.request = self.request
        previous = sys.modules.get("client")
        sys.modules["client"] = client

        def restore():
            if previous is None:
                sys.modules.pop("client", None)
            else:
                sys.modules["client"] = previous

        self.addCleanup(restore)

    def request(self, path, *, method="GET", payload=None, timeout=15):
        self.calls.append((method, path, payload))
        if path == "/services":
            if self.service_override is not None:
                return {
                    "services": self.service_override,
                    "pending_restart": self.settings_pending_restart,
                }
            return {
                "services": [
                    {
                        "id": "trino",
                        "title": "Trino",
                        "state": "available",
                        "description": "固定SQLの実行状況",
                        "profile": "",
                        "enabled": True,
                        "configured": True,
                        "missing_settings": [],
                        "url": "http://localhost:8080/ui/",
                    },
                    {
                        "id": "qdrant",
                        "title": "Qdrant",
                        "state": "unconfigured",
                        "description": "ベクトル索引",
                        "profile": "vector",
                        "enabled": True,
                        "configured": False,
                        "missing_settings": ["QDRANT_API_KEY"],
                        "url": "http://localhost:6333/dashboard",
                    },
                    {
                        "id": "mcp",
                        "title": "MCP / AIクライアント",
                        "state": "available",
                        "description": "読み取りtoolの接続先",
                        "profile": "mcp",
                        "profiles": ["mcp"],
                        "enabled": True,
                        "configured": True,
                        "missing_settings": [],
                        "url": "http://localhost:8503/mcp",
                    },
                ],
                "pending_restart": False,
            }
        if path == "/capabilities":
            return {
                "version": 1,
                "capabilities": [
                    {
                        "id": "catalog",
                        "method": "GET",
                        "path": "/catalog",
                        "access": "read",
                        "description": "公開のデータ契約",
                    }
                ],
            }
        if path == "/catalog":
            return {
                "version": 1,
                "datasets": [
                    {
                        "id": "production",
                        "title": "工場・日別生産",
                        "table": "iceberg.gold.factory_daily_production",
                        "grain": "工場とUTC日付ごとに1行",
                        "primary_key": ["factory_id", "production_date"],
                        "columns": [
                            {
                                "name": "good_qty",
                                "type": "BIGINT",
                                "description": "良品数",
                            }
                        ],
                        "provenance": ["medallion/03_gold.sql"],
                    }
                ],
            }
        if path.startswith("/catalog/preview?"):
            return {
                "datasets": [
                    {
                        "table": "iceberg.gold.factory_daily_production",
                        "snapshot_id": "123",
                        "rows": [{"good_qty": 10}],
                    }
                ]
            }
        if path == "/quality":
            return {
                "datasets": [
                    {
                        "dataset": "mes_events",
                        "bronze_rows": 5,
                        "silver_rows": 4,
                        "quarantine_rows": 1,
                    }
                ],
                "description": "表間で同じ時点を保証しません。",
            }
        if path == "/help":
            return {
                "documents": [
                    {
                        "slug": "proxy",
                        "title": "会社proxy",
                        "description": "接続の切り分け",
                        "available": True,
                    }
                ],
                "assets": [
                    {
                        "id": "architecture",
                        "title": "構成図",
                        "available": True,
                    }
                ],
            }
        if path == "/help/ssrf":
            return {
                "slug": "ssrf",
                "title": "SSRFの接続制限",
                "markdown": (
                    "# SSRFの接続制限\nNO_PROXYは経路、SSRFは接続許可です。"
                ),
            }
        if path == "/help/proxy":
            return {
                "slug": "proxy",
                "title": "会社proxy",
                "markdown": "# 会社proxy\n同梱ヘルプの確認。\n"
                "![remote](https://outside.invalid/test.svg)",
            }
        if path == "/help/assets/architecture":
            return {
                "id": "architecture",
                "title": "構成図",
                "content": '<svg xmlns="http://www.w3.org/2000/svg" '
                'width="100" height="40"><text x="2" y="20">'
                "offline</text></svg>",
            }
        if path == "/jobs?limit=20":
            return {
                "jobs": [
                    {
                        "id": "012345abcdef",
                        "kind": "source",
                        "status": "succeeded",
                        "completed": 1,
                        "batches": 1,
                        "results": [],
                        "error": None,
                    }
                ],
                "persistent": False,
            }
        if path == "/jobs/012345abcdef":
            return {
                "id": "012345abcdef",
                "kind": "source",
                "status": "succeeded",
                "completed": 1,
                "batches": 1,
                "results": [],
                "error": None,
            }
        if path.startswith("/network/config?"):
            return {
                "configuration": {
                    "variables": {},
                    "internal_bypass": {},
                    "custom_ca_configured": False,
                },
                "configuration_scope": "saved"
                if "true" in path
                else "applied",
                "pending_restart": "true" in path,
            }
        if path == "/network/checks" and method == "POST":
            return {
                "checks": [
                    {
                        "target": "pypi",
                        "via_proxy": True,
                        "ok": True,
                        "code": "ok",
                    }
                ],
                "configuration_scope": "saved"
                if payload["use_saved"]
                else "applied",
            }
        if path == "/scenarios":
            return {
                "version": 1,
                "scenarios": [],
                "presets": [],
                "conflicts": [],
            }
        if path == "/settings" and method == "GET":
            return {
                "values": {
                    "N8N_SSRF_PROTECTION_ENABLED": "true",
                    "N8N_SSRF_BLOCKED_IP_RANGES": "default,100.64.0.0/10",
                    "N8N_SSRF_ALLOWED_IP_RANGES": "",
                    "N8N_SSRF_ALLOWED_HOSTNAMES": "worker",
                    "N8N_SSRF_BLOCKED_HOSTNAMES": "",
                    "N8N_SSRF_DNS_CACHE_MAX_SIZE": "1048576",
                    **self.settings_values,
                },
                "secret_keys": [],
                "configured_secrets": [],
                "pending_restart": self.settings_pending_restart,
            }
        if path == "/settings" and method == "PATCH":
            self.settings_pending_restart = True
            return {"saved": True, "applied": False, "pending_restart": True}
        if path == "/status":
            return {"services": {"worker": {"healthy": True, "busy": False}}}
        raise AssertionError("Unexpected portal call: " + method + " " + path)

    def app(self):
        app = AppTest.from_file(str(ROOT / "infra/demo/app.py")).run(
            timeout=10
        )
        self.assertEqual(len(app.exception), 0)
        return app

    def public_card(self, *, service="trino", url="", configured=False):
        """Return an internally healthy card with a separate browser target."""
        return {
            "id": service,
            "title": "MCP / AIクライアント" if service == "mcp" else "Trino",
            "state": "available",
            "description": "合成の内部health確認",
            "profile": "mcp" if service == "mcp" else "",
            "enabled": True,
            "configured": True,
            "missing_settings": [],
            "url": url,
            "link_mode": "public",
            "link_configured": configured,
        }

    def test_public_link_uses_supplied_https_url_and_keeps_internal_health(
        self,
    ):
        self.service_override = [
            self.public_card(
                url="https://trino.example.invalid/ui/", configured=True
            )
        ]
        app = self.app()
        links = app.get("link_button")
        self.assertEqual(len(links), 1)
        self.assertEqual(
            links[0].proto.url, "https://trino.example.invalid/ui/"
        )
        self.assertFalse(links[0].proto.disabled)
        self.assertTrue(
            any("内部状態: 稼働を確認" in item.value for item in app.markdown)
        )
        self.assertTrue(
            any("外部公開URL" in item.value for item in app.caption)
        )
        self.assertFalse(any("localhost" in item.proto.url for item in links))

    def test_public_missing_url_is_inert_without_localhost_fallback(self):
        self.service_override = [self.public_card()]
        app = self.app()
        self.assertEqual(len(app.get("link_button")), 0)
        self.assertTrue(
            app.button(key="portal-link-unconfigured-trino").disabled
        )
        self.assertTrue(
            any("外部URL未設定" in item.value for item in app.caption)
        )
        self.assertTrue(
            any("内部状態: 稼働を確認" in item.value for item in app.markdown)
        )

    def test_unconfigured_link_flag_wins_over_an_inconsistent_url(self):
        self.service_override = [
            self.public_card(url="http://localhost:8080/ui/")
        ]
        app = self.app()
        self.assertEqual(len(app.get("link_button")), 0)
        self.assertTrue(
            app.button(key="portal-link-unconfigured-trino").disabled
        )

    def test_public_mcp_missing_url_has_no_empty_or_local_code_block(self):
        self.service_override = [self.public_card(service="mcp")]
        app = self.app()
        self.assertFalse(any(not item.value.strip() for item in app.code))
        self.assertEqual(
            [
                item.value
                for item in app.code
                if item.value.startswith(("http://", "https://"))
            ],
            [],
        )
        self.assertTrue(
            any("外部URL未設定" in item.value for item in app.caption)
        )

    def test_public_mcp_configured_url_is_the_ai_client_target(self):
        self.service_override = [
            self.public_card(
                service="mcp",
                url="https://mcp.example.invalid/mcp",
                configured=True,
            )
        ]
        app = self.app()
        self.assertEqual(
            [
                item.value
                for item in app.code
                if item.value.startswith(("http://", "https://"))
            ],
            ["https://mcp.example.invalid/mcp"],
        )

    def test_native_link_does_not_turn_unreachable_internal_health_green(
        self,
    ):
        card = self.public_card(
            url="https://trino.example.invalid/ui/", configured=True
        )
        card["state"] = "unreachable"
        self.service_override = [card]
        app = self.app()
        self.assertTrue(
            any(
                "停止・起動待ち・未到達" in item.value for item in app.markdown
            )
        )
        self.assertFalse(app.get("link_button")[0].proto.disabled)

    def test_public_access_settings_save_choices_without_applying_links(self):
        self.settings_values = {
            "PORTAL_LINK_MODE": "local",
            "TRINO_PUBLIC_URL": "",
            "DEMO_GUI_PUBLIC_URL": "",
            "MARQUEZ_UI_PUBLIC_URL": "",
        }
        # The service cards still describe the API's applied configuration.
        self.service_override = [
            {
                **self.public_card(),
                "link_mode": "local",
                "link_configured": True,
                "url": "http://localhost:8080/ui/",
            }
        ]
        app = self.app()
        self.assertTrue(
            any(item.label == "外部アクセス" for item in app.expander)
        )
        mode = app.selectbox(key="settings-portal-link-mode")
        self.assertEqual(mode.value, "local")
        self.assertIn("外部公開URL（HTTPS）", mode.options)
        captions = " ".join(item.value for item in app.caption)
        self.assertIn("Tunnel・Accessのルート", captions)
        self.assertIn("自動で付きます", captions)
        self.assertTrue(
            any(
                "PoCポータルの公開URL" in item.label for item in app.text_input
            )
        )
        self.assertTrue(
            any("Marquezの公開URL" in item.label for item in app.text_input)
        )
        mode.select("public")
        app.text_input(key="settings-public-url-TRINO_PUBLIC_URL").set_value(
            "https://trino.example.invalid"
        )
        next(
            item for item in app.button if item.label == "設定を保存"
        ).click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(
            (
                "PATCH",
                "/settings",
                {
                    "values": {
                        "PORTAL_LINK_MODE": "public",
                        "TRINO_PUBLIC_URL": "https://trino.example.invalid",
                    }
                },
            ),
            self.calls,
        )
        app.run(timeout=10)
        self.assertEqual(
            app.get("link_button")[0].proto.url,
            "http://localhost:8080/ui/",
        )
        self.assertTrue(
            any("再配置までは" in item.value for item in app.warning)
        )
        self.assertFalse(any(method == "POST" for method, _, _ in self.calls))

    def test_initial_portal_shows_states_and_only_reads_fixed_metadata(self):
        app = self.app()
        content = " ".join(item.value for item in app.markdown)
        self.assertIn("稼働を確認", content)
        self.assertIn("必要設定が未完了", content)
        self.assertIn(
            "QDRANT_API_KEY", " ".join(item.value for item in app.caption)
        )
        self.assertIn(
            "http://localhost:8503/mcp", [item.value for item in app.code]
        )
        links = {item.proto.url: item for item in app.get("link_button")}
        self.assertFalse(links["http://localhost:8080/ui/"].proto.disabled)
        self.assertTrue(
            links["http://localhost:6333/dashboard"].proto.disabled
        )
        self.assertEqual([call for call in self.calls if call[0] != "GET"], [])

    def test_settings_opens_only_the_bundled_ssrf_manual(self):
        app = self.app()
        self.assertTrue(
            any(item.label == "接続制限（SSRF）" for item in app.expander)
        )
        protection = next(
            item
            for item in app.text_input
            if item.label == "n8nのSSRF保護（true/false）"
        )
        self.assertEqual(protection.value, "true")
        self.assertTrue(
            any("NO_PROXYはproxyを迂回" in item.value for item in app.caption)
        )
        app.button(key="settings-ssrf-help").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(("GET", "/help/ssrf", None), self.calls)
        self.assertTrue(
            any("NO_PROXYは経路" in item.value for item in app.markdown)
        )
        self.assertEqual([call for call in self.calls if call[0] != "GET"], [])

    def test_preview_and_quality_are_explicit_fixed_read_operations(self):
        app = self.app()
        app.button(key="portal-preview").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(
            ("GET", "/catalog/preview?dataset=production&limit=20", None),
            self.calls,
        )
        app.button(key="portal-quality").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(("GET", "/quality", None), self.calls)
        self.assertEqual([call for call in self.calls if call[0] != "GET"], [])

    def test_bundled_help_and_svg_are_requested_without_browser_image_urls(
        self,
    ):
        app = self.app()
        app.button(key="portal-open-help").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(("GET", "/help/proxy", None), self.calls)
        self.assertFalse(
            any("outside.invalid" in item.value for item in app.markdown)
        )
        app.button(key="portal-open-asset").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(("GET", "/help/assets/architecture", None), self.calls)

    def test_network_scope_is_explicit_and_job_history_only_restores_display(
        self,
    ):
        app = self.app()
        app.checkbox(key="network-use-saved").check().run(timeout=10)
        app.multiselect(key="network-targets").set_value(["pypi"]).run(
            timeout=10
        )
        app.button(key="network-checks").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"],
            [
                (
                    "POST",
                    "/network/checks",
                    {"targets": ["pypi"], "use_saved": True},
                )
            ],
        )
        app.button(key="portal-show-job").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertIn(("GET", "/jobs/012345abcdef", None), self.calls)
        self.assertFalse(
            any(path.startswith("/pipeline/") for _, path, _ in self.calls)
        )


if __name__ == "__main__":
    unittest.main()
