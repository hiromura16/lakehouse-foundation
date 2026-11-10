"""Exercise real Streamlit widgets without SQL, network, or worker calls."""

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
APP = ROOT / "infra/demo/app.py"


class DemoSourceUiTests(unittest.TestCase):
    """Check scenario choices, dedicated-DB consent and source-only calls."""

    def setUp(self):
        self.calls = []
        self.pending_restart = False
        self.result_status = "running"
        self.job_kind = "cdc-source"
        self.job_results = []
        self.catalog = {
            "version": 1,
            "scenarios": [
                {
                    "id": "invalid_integer",
                    "label": "数量の型不正",
                    "description": "数量へ数値に変換できない文字を入れます。",
                    "category": "型と業務値",
                    "targets": ["mes_events"],
                    "supported_destinations": ["landing", "sqlserver", "both"],
                    "expected_effect": "Silverから隔離されます。",
                    "requires_file_consent": False,
                    "requires_images": False,
                },
                {
                    "id": "hash_mismatch",
                    "label": "完了マーカーのhash不一致",
                    "description": "完成済みファイルと異なるhashにします。",
                    "category": "ファイル異常",
                    "targets": ["ready"],
                    "supported_destinations": ["landing", "both"],
                    "expected_effect": "取り込みを拒否します。",
                    "requires_file_consent": True,
                    "requires_images": False,
                },
                {
                    "id": "corrupt_image",
                    "label": "画像を破損させる",
                    "description": "PNGの内容を破損させます。",
                    "category": "ファイル異常",
                    "targets": ["images"],
                    "supported_destinations": ["landing", "both"],
                    "expected_effect": "画像の内容検査で検出します。",
                    "requires_file_consent": True,
                    "requires_images": True,
                },
            ],
            "conflicts": [["hash_mismatch", "corrupt_image"]],
            "presets": [
                {
                    "id": "type_errors",
                    "label": "型不正の確認",
                    "base_scenario": "clean",
                    "anomalies": ["invalid_integer"],
                }
            ],
        }
        client = types.ModuleType("client")
        client.request = self.request
        previous = sys.modules.get("client")
        sys.modules["client"] = client

        def restore_client():
            # Preserve modules loaded by Streamlit, including native NumPy.
            if previous is None:
                sys.modules.pop("client", None)
            else:
                sys.modules["client"] = previous

        self.addCleanup(restore_client)

    def request(self, path, *, method="GET", payload=None, timeout=15):
        """Stand in for the server API while retaining every UI request."""
        self.calls.append((method, path, payload))
        if path == "/settings":
            return {
                "values": {},
                "secret_keys": [],
                "configured_secrets": [],
                "pending_restart": False,
                "pending_mount_change": False,
            }
        if path == "/services":
            return {"services": [], "pending_restart": False}
        if path == "/capabilities":
            return {"version": 1, "capabilities": []}
        if path == "/catalog":
            return {"version": 1, "datasets": []}
        if path == "/help":
            return {"documents": [], "assets": []}
        if path == "/jobs?limit=20":
            return {"jobs": [], "persistent": False}
        if path.startswith("/network/config?"):
            return {"configuration": {}, "configuration_scope": "applied"}
        if path == "/scenarios":
            return self.catalog
        if path == "/jobs" and method == "POST":
            self.job_kind = "source"
            return {
                "id": "012345abcdef",
                "status": "queued",
                "kind": self.job_kind,
                "pending_restart": self.pending_restart,
            }
        if path == "/status":
            return {"services": {}}
        if path.startswith("/cdc/demo/") and method == "POST":
            return {
                "id": "012345abcdef",
                "status": "queued",
                "kind": "cdc-source",
                "pending_restart": self.pending_restart,
            }
        if path == "/jobs/012345abcdef" and method == "GET":
            return {
                "id": "012345abcdef",
                "kind": self.job_kind,
                "status": self.result_status,
                "completed": int(self.result_status == "succeeded"),
                "batches": 1,
                "pending_restart": self.pending_restart,
                "results": self.job_results,
                "error": None,
            }
        raise AssertionError("Unexpected API request: " + method + " " + path)

    def app(self):
        """Run the actual page with a fake server-side API transport."""
        app = AppTest.from_file(str(APP)).run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        return app

    def submit_button(self, app):
        """Find the source submission without relying on widget order."""
        return next(
            button
            for button in app.button
            if button.label == "素材の投入を開始"
        )

    def test_default_source_request_preserves_legacy_quality_and_no_anomalies(
        self,
    ):
        app = self.app()
        self.submit_button(app).click().run(timeout=10)
        posts = [call for call in self.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/jobs")
        self.assertEqual(posts[0][2]["scenario"], "edge")
        self.assertEqual(posts[0][2]["anomalies"], [])
        self.assertEqual(posts[0][2]["anomaly_rate"], 10)
        self.assertFalse(posts[0][2]["allow_file_faults"])
        self.assertFalse(
            any(path.startswith("/pipeline/") for _, path, _ in self.calls)
        )

    def test_preset_and_rate_are_visible_and_forwarded_without_a_pipeline(
        self,
    ):
        app = self.app()
        app.selectbox(key="source-preset").select("type_errors").run(
            timeout=10
        )
        self.assertEqual(
            app.multiselect(key="source-anomalies").value, ["invalid_integer"]
        )
        self.assertEqual(
            app.selectbox(key="source-base-scenario").value, "clean"
        )
        app.slider(key="anomaly-rate").set_value(35).run(timeout=10)
        self.assertTrue(
            any(
                "期待する下流の挙動" in frame.value.columns
                for frame in app.dataframe
            )
        )
        self.submit_button(app).click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        posts = [call for call in self.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], "/jobs")
        self.assertEqual(posts[0][2]["anomalies"], ["invalid_integer"])
        self.assertEqual(posts[0][2]["anomaly_rate"], 35)
        self.assertEqual(posts[0][2]["scenario"], "clean")

    def test_file_fault_requires_consent_and_rejects_sql_only_before_post(
        self,
    ):
        app = self.app()
        app.multiselect(key="source-anomalies").set_value(
            ["hash_mismatch"]
        ).run(timeout=10)
        self.assertTrue(self.submit_button(app).disabled)
        app.checkbox(key="allow-file-faults").check().run(timeout=10)
        self.assertFalse(self.submit_button(app).disabled)
        destination = next(
            box for box in app.selectbox if box.label == "投入先"
        )
        destination.select("専用の検証SQL Server").run(timeout=10)
        self.assertTrue(self.submit_button(app).disabled)
        self.assertTrue(
            any(
                "現在の投入先では使えません" in item.value
                for item in app.error
            )
        )
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"], []
        )

    def test_corrupt_image_requires_images_and_resets_changed_file_consent(
        self,
    ):
        app = self.app()
        app.multiselect(key="source-anomalies").set_value(
            ["hash_mismatch"]
        ).run(timeout=10)
        app.checkbox(key="allow-file-faults").check().run(timeout=10)
        app.multiselect(key="source-anomalies").set_value(
            ["corrupt_image"]
        ).run(timeout=10)
        self.assertFalse(app.checkbox(key="allow-file-faults").value)
        app.checkbox(key="allow-file-faults").check().run(timeout=10)
        self.assertTrue(self.submit_button(app).disabled)
        images = next(
            box
            for box in app.checkbox
            if box.label == "合成の検査画像PNGも作る"
        )
        images.check().run(timeout=10)
        self.assertFalse(self.submit_button(app).disabled)
        self.submit_button(app).click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        posts = [call for call in self.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["anomalies"], ["corrupt_image"])
        self.assertTrue(posts[0][2]["allow_file_faults"])
        self.assertTrue(posts[0][2]["images"])

    def test_conflicting_anomalies_are_explained_before_submission(self):
        app = self.app()
        app.multiselect(key="source-anomalies").set_value(
            ["hash_mismatch", "corrupt_image"]
        ).run(timeout=10)
        app.checkbox(key="allow-file-faults").check().run(timeout=10)
        images = next(
            box
            for box in app.checkbox
            if box.label == "合成の検査画像PNGも作る"
        )
        images.check().run(timeout=10)
        self.assertTrue(self.submit_button(app).disabled)
        self.assertTrue(
            any("競合して変更" in item.value for item in app.error)
        )
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"], []
        )

    def test_intentional_fault_results_show_paths_and_expected_ingest_effects(
        self,
    ):
        self.result_status = "succeeded"
        self.job_results = [
            {
                "batch": 1,
                "manifest": "manifests/demo__012345abcdef_0001.json",
                "diagnostics": {
                    "anomalies": ["hash_mismatch", "invalid_integer"],
                    "anomaly_rate": 35,
                    "row_changes": [
                        {
                            "id": "invalid_integer",
                            "table": "mes_events",
                            "affected": 2,
                        }
                    ],
                    "file_faults": [
                        {
                            "id": "hash_mismatch",
                            "path": "csv/erp_products__012345abcdef_0001.csv",
                            "expected_effect": "取り込みを拒否します。",
                        }
                    ],
                    "intentional_file_faults": True,
                },
            }
        ]
        app = self.app()
        self.submit_button(app).click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        self.assertTrue(
            any("生成ジョブの成功" in item.value for item in app.warning)
        )
        columns = [set(frame.value.columns) for frame in app.dataframe]
        self.assertTrue(any("変更対象件数" in item for item in columns))
        self.assertTrue(any("対象ファイル" in item for item in columns))
        self.assertIsNone(app.session_state["active_job"])

    def test_real_catalog_presets_render_and_keep_category_selections(self):
        specification = importlib.util.spec_from_file_location(
            "ui_real_scenarios", ROOT / "scripts/source_scenarios.py"
        )
        engine = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(engine)
        self.catalog = {
            "version": 1,
            "scenarios": engine.catalog(),
            "presets": engine.presets(),
            "conflicts": [sorted(group) for group in engine.CONFLICTS],
        }
        app = self.app()
        for preset in self.catalog["presets"]:
            with self.subTest(preset=preset["id"]):
                app.selectbox(key="source-preset").select(preset["id"]).run(
                    timeout=10
                )
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(
                    app.multiselect(key="source-anomalies").value,
                    preset["anomalies"],
                )
                self.assertEqual(
                    app.selectbox(key="source-base-scenario").value,
                    preset["base_scenario"],
                )
        before = app.multiselect(key="source-anomalies").value
        category = next(
            box for box in app.selectbox if box.label == "異常のカテゴリ"
        )
        category.select("row").run(timeout=10)
        self.assertEqual(app.multiselect(key="source-anomalies").value, before)
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"], []
        )

    def test_cdc_buttons_are_disabled_until_explicit_consent(self):
        app = self.app()
        self.assertFalse(app.checkbox(key="cdc-dml-confirm").value)
        for action in ("insert", "update", "delete"):
            self.assertTrue(app.button(key="cdc-" + action).disabled)
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"], []
        )
        app.checkbox(key="cdc-dml-confirm").check().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        for action in ("insert", "update", "delete"):
            self.assertFalse(app.button(key="cdc-" + action).disabled)
        self.assertEqual(
            [call for call in self.calls if call[0] == "POST"], []
        )

    def test_three_actions_send_fixed_routes_and_poll_without_a_pipeline(self):
        for action in ("insert", "update", "delete"):
            with self.subTest(action=action):
                self.calls.clear()
                app = self.app()
                app.checkbox(key="cdc-dml-confirm").check().run(timeout=10)
                app.button(key="cdc-" + action).click().run(timeout=10)
                self.assertEqual(len(app.exception), 0)
                self.assertEqual(
                    [call for call in self.calls if call[0] == "POST"],
                    [("POST", "/cdc/demo/" + action, {"confirm": True})],
                )
                self.assertIn(("GET", "/jobs/012345abcdef", None), self.calls)
                self.assertEqual(
                    app.session_state["active_job"], "012345abcdef"
                )
                self.assertEqual(app.session_state["job_kind"], "cdc-source")
                self.assertFalse(
                    any(
                        path.startswith("/pipeline/")
                        for _, path, _ in self.calls
                    )
                )
                self.assertFalse(
                    any(
                        button.label == "次のマイクロバッチから停止する"
                        for button in app.button
                    )
                )
                self.assertTrue(
                    any(
                        "途中取消できません" in item.value for item in app.info
                    )
                )

    def test_cdc_progress_warns_that_saved_settings_are_not_active(self):
        self.pending_restart = True
        app = self.app()
        app.checkbox(key="cdc-dml-confirm").check().run(timeout=10)
        app.button(key="cdc-insert").click().run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(
            any("保存した設定は未反映" in item.value for item in app.warning)
        )


if __name__ == "__main__":
    unittest.main()
