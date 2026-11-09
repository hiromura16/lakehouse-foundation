"""Use real portal widgets while keeping all transport local and mocked."""

import sys
import types
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]


class PortalUiTests(unittest.TestCase):
    """Confirm portal reads and explicit checks never run a pipeline."""

    def setUp(self):
        self.calls = []
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
        if path == "/settings":
            return {
                "values": {
                    "N8N_SSRF_PROTECTION_ENABLED": "true",
                    "N8N_SSRF_BLOCKED_IP_RANGES": "default,100.64.0.0/10",
                    "N8N_SSRF_ALLOWED_IP_RANGES": "",
                    "N8N_SSRF_ALLOWED_HOSTNAMES": "worker",
                    "N8N_SSRF_BLOCKED_HOSTNAMES": "",
                    "N8N_SSRF_DNS_CACHE_MAX_SIZE": "1048576",
                },
                "secret_keys": [],
                "configured_secrets": [],
            }
        if path == "/status":
            return {"services": {"worker": {"healthy": True, "busy": False}}}
        raise AssertionError("Unexpected portal call: " + method + " " + path)

    def app(self):
        app = AppTest.from_file(str(ROOT / "infra/demo/app.py")).run(
            timeout=10
        )
        self.assertEqual(len(app.exception), 0)
        return app

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
