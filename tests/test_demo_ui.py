"""Exercise real Streamlit widgets without SQL, network, or worker calls."""

import sys
import types
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "infra/demo/app.py"


class CdcSourceUiTests(unittest.TestCase):
    """Check dedicated-DB consent and the source-only API boundary."""

    def setUp(self):
        self.calls = []
        self.pending_restart = False
        self.result_status = "running"
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
                "kind": "cdc-source",
                "status": self.result_status,
                "completed": int(self.result_status == "succeeded"),
                "batches": 1,
                "pending_restart": self.pending_restart,
                "results": [],
                "error": None,
            }
        raise AssertionError("Unexpected API request: " + method + " " + path)

    def app(self):
        """Run the actual page with a fake server-side API transport."""
        app = AppTest.from_file(str(APP)).run(timeout=10)
        self.assertEqual(len(app.exception), 0)
        return app

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
