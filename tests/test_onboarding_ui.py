"""Exercise onboarding widgets and stale-evidence rules without a server.

AppTest runs the actual Streamlit script, not a replacement widget API.
Metadata upload parsing is tested directly because AppTest has no supported
file-uploader setter in the pinned Streamlit release.
"""

import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    from streamlit.testing.v1 import AppTest
except ModuleNotFoundError as error:
    if (
        error.name != "streamlit"
        or importlib.util.find_spec("streamlit") is not None
    ):
        raise
    raise unittest.SkipTest(
        "Optional UI tests need the hash-locked requirements-dev.lock."
    ) from error

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "infra/demo"
APP = DEMO / "onboarding_preview.py"
with mock.patch.object(sys, "path", [str(DEMO), *sys.path]):
    UI = importlib.import_module("onboarding_ui")
    CORE = importlib.import_module("onboarding_contract")


class DiscoveryUploadTests(unittest.TestCase):
    """Keep local upload rejection bounded and free of rejected values."""

    def test_valid_metadata_is_not_interpreted_as_yaml_or_a_path(self):
        value = CORE.builtin_discovery()
        self.assertEqual(
            UI.parse_discovery_upload(json.dumps(value).encode()), value
        )

    def test_size_type_json_duplicates_constants_and_depth_are_rejected(self):
        cases = [
            "not-bytes",
            b"x" * (UI.MAX_METADATA_BYTES + 1),
            b"not-json",
            b"\xff",
            b"[]",
            b'{"a":1,"a":2}',
            b'{"a":{"b":1,"b":2}}',
            b'{"a":NaN}',
            b'{"a":Infinity}',
            b"[" * 2000 + b"]" * 2000,
        ]
        for payload in cases:
            with self.subTest(payload_type=type(payload).__name__):
                with self.assertRaises(ValueError):
                    UI.parse_discovery_upload(payload)

    def test_rejected_content_is_not_echoed_in_parse_errors(self):
        sentinel = "synthetic-private-fragment"
        with self.assertRaises(ValueError) as raised:
            UI.parse_discovery_upload((sentinel + " not JSON").encode())
        self.assertNotIn(sentinel, str(raised.exception))


class OnboardingUiTests(unittest.TestCase):
    """Check real widget selections, downloads and draft-only review notes."""

    def new_app(self):
        app = AppTest.from_file(str(APP)).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        return app

    def loaded_app(self):
        app = self.new_app()
        app.button(key="onboarding-example").click().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        return app

    def proposed_app(self):
        app = self.loaded_app()
        app.button(key="onboarding-propose").click().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        return app

    def test_initial_render_is_idle_and_has_no_network_or_example_lookup(self):
        with mock.patch.object(
            CORE, "builtin_discovery", side_effect=AssertionError("not yet")
        ) as example:
            app = self.new_app()
        example.assert_not_called()
        self.assertTrue(app.button(key="onboarding-import").disabled)
        self.assertEqual(len(app.get("download_button")), 0)
        self.assertTrue(any("YAML" in item.value for item in app.markdown))

    def test_example_lists_six_tables_and_detected_key_and_types(self):
        app = self.loaded_app()
        tables = next(
            item
            for item in app.multiselect
            if item.key.startswith("onboarding-tables-")
        )
        self.assertEqual(len(tables.options), 6)
        self.assertEqual(tables.value, ["lh_demo_erp.products"])
        frame = app.dataframe[0].value
        self.assertIn("SQL型", frame.columns)
        self.assertTrue(
            frame.loc[frame["列"] == "product_id", "主キー候補"].all()
        )

    def test_business_choices_flow_into_the_generated_private_contract(self):
        app = self.loaded_app()
        app.text_input(key="onboarding-owner-team").set_value("業務チーム")
        purpose = next(
            item for item in app.text_input if item.key.endswith("-purpose")
        )
        purpose.set_value("製造計画の照合")
        deletion = next(
            item for item in app.selectbox if item.key.endswith("-delete")
        )
        deletion.select("retain")
        app.button(key="onboarding-propose").click().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        result = app.session_state["onboarding-result-proposal"]
        self.assertEqual(result["contract"]["owner_team"], "業務チーム")
        table = result["contract"]["tables"][0]
        self.assertEqual(table["business_purpose"], "製造計画の照合")
        self.assertEqual(table["delete_handling"], "retain")
        self.assertFalse(result["can_apply"])
        self.assertFalse(result["approvals_verified"])

    def test_empty_selection_cannot_submit_a_contract(self):
        app = self.loaded_app()
        tables = next(
            item
            for item in app.multiselect
            if item.key.startswith("onboarding-tables-")
        )
        tables.set_value([]).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.button(key="onboarding-propose").disabled)

    def test_all_six_tables_can_be_assembled_without_text_configuration(self):
        app = self.loaded_app()
        tables = next(
            item
            for item in app.multiselect
            if item.key.startswith("onboarding-tables-")
        )
        tables.set_value(list(tables.options)).run(timeout=20)
        app.button(key="onboarding-propose").click().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(
            len(
                app.session_state["onboarding-result-proposal"]["contract"][
                    "tables"
                ]
            ),
            6,
        )

    def test_draft_download_bytes_match_the_displayed_non_applied_result(self):
        app = self.loaded_app()
        original = UI.st.download_button
        with mock.patch.object(
            UI.st, "download_button", wraps=original
        ) as download:
            app.button(key="onboarding-propose").click().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        arguments = download.call_args.kwargs
        exported = json.loads(arguments["data"])
        self.assertEqual(
            exported, app.session_state["onboarding-result-proposal"]
        )
        self.assertEqual(arguments["file_name"], "source-contract-draft.json")
        self.assertFalse(exported["applied"])
        self.assertFalse(exported["can_apply"])

    def test_business_change_clears_plan_reviews_and_both_downloads(self):
        app = self.proposed_app()
        app.checkbox(key="onboarding-review-business").check().run(timeout=20)
        app.button(key="onboarding-plan").click().run(timeout=20)
        self.assertEqual(len(app.get("download_button")), 2)
        app.text_input(key="onboarding-owner-team").set_value(
            "別の担当部門"
        ).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("download_button")), 0)
        self.assertTrue(any("無効" in item.value for item in app.warning))
        app.button(key="onboarding-propose").click().run(timeout=20)
        self.assertFalse(app.checkbox(key="onboarding-review-business").value)

    def test_column_change_invalidates_the_existing_draft(self):
        app = self.proposed_app()
        columns = next(
            item for item in app.multiselect if item.key.endswith("-columns")
        )
        columns.set_value(["product_id"]).run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("download_button")), 0)

    def test_metadata_replacement_invalidates_the_existing_draft(self):
        app = self.proposed_app()
        metadata = app.session_state["onboarding-discovery"]
        replacement = json.loads(json.dumps(metadata))
        replacement["source_ref"] = "other-synthetic-source"
        app.session_state["onboarding-discovery"] = replacement
        app.run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("download_button")), 0)

    def test_review_change_invalidates_only_plan_not_the_current_proposal(
        self,
    ):
        app = self.proposed_app()
        app.button(key="onboarding-plan").click().run(timeout=20)
        self.assertEqual(len(app.get("download_button")), 2)
        app.checkbox(key="onboarding-review-dba").check().run(timeout=20)
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.get("download_button")), 1)
        app.button(key="onboarding-plan").click().run(timeout=20)
        result = app.session_state["onboarding-result-plan"]
        self.assertTrue(result["review"]["dba"])
        self.assertFalse(result["approvals_verified"])
        self.assertFalse(result["can_apply"])
        self.assertFalse(result["database_checked"])
        self.assertFalse(
            any(
                item.label in {"適用", "Apply", "CDC登録", "DBを作成"}
                for item in app.button
            )
        )


if __name__ == "__main__":
    unittest.main()
