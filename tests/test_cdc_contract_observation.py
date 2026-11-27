"""実ネットワークを使わずCDC観測の境界とマスクを検査する。"""

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cdc_contract import table
from test_cdc_contract_registration import FIXTURE, desired, observed

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/cdc_observation.py"
with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
    model = importlib.import_module("cdc_observation")

NOW = 160_000
GENERATION = "f" * 64


def fixture(*, unregistered=False):
    """契約、登録観測、別面のruntime観測を合成する。"""
    intent = desired(table("internal_shape_marker"))
    registration = observed() if unregistered else observed(intent)
    plan = model.assessment(intent, registration, FIXTURE)
    connectors = model.compile_spec(intent)["connectors"]
    group = connectors[0]
    route = group["routes"][0]
    actual = {
        "version": 1,
        "environment": "synthetic",
        "fixture_id": FIXTURE,
        "generation": GENERATION,
        "observed_at": NOW - 1,
        "registration_sha256": plan["observation_sha256"],
        "desired_spec_sha256": plan["desired_spec_sha256"],
        "connectors": [
            {
                "spec_sha256": model.digest(group),
                "state": "running",
                "tasks": ["running"],
                "snapshot": "complete",
            }
        ],
        "tables": [
            {
                "route_sha256": model.digest(route),
                "topic_sha256": model.digest(route["topics"]["canonical"]),
                "group_sha256": model.digest([FIXTURE, group["topic.prefix"]]),
                "sql": {
                    "table_exists": True,
                    "columns_match": True,
                    "primary_key_match": True,
                    "database_cdc_enabled": True,
                    "table_cdc_enabled": True,
                    "capture_visible": True,
                    "capture_count": 1,
                },
                "partition_count": 1,
                "partitions": [
                    {
                        "partition": 0,
                        "broker_next": 11,
                        "local_next": 11,
                        "broker_low": 9,
                        "broker_high": 15,
                    }
                ],
                "targets": {
                    kind: {
                        "identity_sha256": model.digest(
                            {
                                "kind": kind,
                                **route["destinations"][kind],
                            }
                        ),
                        "snapshot_id": i,
                        "row_count": i + 10,
                    }
                    for i, kind in enumerate(model.KINDS, start=1)
                },
                "last_event_age_seconds": 17,
                "last_error": "none",
            }
        ],
    }
    return intent, registration, actual


def describe(inputs):
    """同じscope・generation・時刻の診断を得る。"""
    return model.report(*inputs, FIXTURE, GENERATION, NOW)


class ObservationTests(unittest.TestCase):
    """完了位置、未観測、相違を同一状態に丸めない。"""

    def test_consistent_observation_reports_real_numbers_only(self):
        """受理できた観測だけをlagやIceberg情報と一緒に返す。"""
        report = describe(fixture())
        self.assertEqual(report["status"], "observed_aligned")
        self.assertEqual(report["observation_age_seconds"], 1)
        self.assertEqual(report["connectors"][0]["tasks"], ["running"])
        self.assertEqual(
            report["tables"][0]["targets"]["raw"],
            {"snapshot_id": 1, "row_count": 11},
        )
        self.assertEqual(
            report["tables"][0]["partitions"][0],
            {
                "partition": 0,
                "broker_next": 11,
                "local_next": 11,
                "broker_low": 9,
                "broker_high": 15,
                "broker_lag": 4,
                "state": "aligned",
            },
        )
        self.assertEqual(
            report["current_structure"], report["desired_structure"]
        )
        self.assertEqual(report["diagnostics"], [])

    def test_new_registration_not_a_confirmed_runtime(self):
        """提示された値が揃っても、新規登録候補を実行許可としない。"""
        output = describe(fixture(unregistered=True))
        self.assertEqual(output["status"], "proposal_only")
        self.assertIsNone(output["current_spec_sha256"])
        self.assertTrue(output["proposal"]["steps"])
        for name in (
            "writes_available",
            "offset_reset_available",
            "restart_is_proposed",
            "snapshot_is_requested",
        ):
            self.assertIs(output["proposal"][name], False)
        self.assertNotIn("approval_candidate_sha256", json.dumps(output))

    def test_either_order_position_mismatch_has_read_only_help(self):
        """brokerとlocalのどちらが進んでも補正せず、順序を推測しない。"""
        for local in (10, 12):
            inputs = fixture()
            position = inputs[2]["tables"][0]["partitions"][0]
            position["local_next"] = local
            output = describe(inputs)
            self.assertEqual(output["status"], "blocked")
            self.assertIn(
                {"code": "POSITION_MISMATCH", "table": 1, "partition": 0},
                output["diagnostics"],
            )
            self.assertEqual(len(output["position_mismatch_help"]), 4)
            self.assertIn(
                "DO_NOT_RESET_OFFSETS_OR_RECREATE_STATE",
                output["position_mismatch_help"],
            )

    def test_group_topic_and_target_mismatch_hide_foreign_values(self):
        """関連付けに失敗した座標や別保存先の集計は出力しない。"""
        inputs = fixture()
        row = inputs[2]["tables"][0]
        row["group_sha256"] = "0" * 64
        row["topic_sha256"] = "0" * 64
        row["partitions"][0]["local_next"] = 10
        row["targets"]["raw"]["identity_sha256"] = "0" * 64
        result = describe(inputs)
        self.assertEqual(result["status"], "blocked")
        self.assertIsNone(result["tables"][0]["partitions"])
        self.assertEqual(result["position_mismatch_help"], [])
        self.assertEqual(
            result["tables"][0]["targets"]["raw"],
            {"snapshot_id": None, "row_count": None},
        )

    def test_unknowns_and_cdc_visibility_not_filled_in(self):
        """捕捉、Kafka位置、runtime taskの観測不能を明示する。"""
        inputs = fixture()
        row = inputs[2]["tables"][0]
        row["sql"]["capture_visible"] = None
        row["sql"]["capture_count"] = None
        row["partitions"] = None
        row["last_event_age_seconds"] = None
        inputs[2]["connectors"][0]["tasks"] = None
        output = describe(inputs)
        self.assertEqual(output["status"], "blocked")
        codes = {item["code"] for item in output["diagnostics"]}
        self.assertLessEqual(
            {
                "SQL_CAPTURE_VISIBLE_UNKNOWN",
                "SQL_CAPTURE_COUNT_UNKNOWN",
                "TASKS_UNKNOWN",
                "POSITIONS_UNKNOWN",
                "LAST_EVENT_AGE_UNKNOWN",
            },
            codes,
        )
        self.assertIsNone(output["tables"][0]["last_event_age_seconds"])

    def test_retained_low_watermark_and_inverted_interval_fail_closed(self):
        """残る最古位置を確認できなければ、位置一致でも合格しない。"""
        for change, reason in (
            ({"broker_low": None}, "BROKER_LOW_UNKNOWN"),
            ({"broker_low": 16}, "BROKER_RETAINED_RANGE_INVERTED"),
            (
                {"broker_next": 8, "local_next": 8},
                "POSITION_BELOW_RETAINED_LOW",
            ),
            ({"broker_next": 8}, "POSITION_BELOW_RETAINED_LOW"),
            ({"local_next": 8}, "POSITION_BELOW_RETAINED_LOW"),
        ):
            inputs = fixture()
            inputs[2]["tables"][0]["partitions"][0].update(change)
            output = describe(inputs)
            self.assertEqual(output["status"], "blocked")
            self.assertIn(reason, [x["code"] for x in output["diagnostics"]])
        inputs = fixture()
        inputs[2]["tables"][0]["partitions"][0].update(broker_low=11)
        self.assertEqual(describe(inputs)["status"], "observed_aligned")

    def test_any_runtime_diagnostic_withholds_registration_steps(self):
        """静的差分があっても、位置ずれ・他の診断で提案手順を伏せる。"""
        for change in ("offset", "connector"):
            inputs = fixture(unregistered=True)
            if change == "offset":
                inputs[2]["tables"][0]["partitions"][0]["local_next"] = 12
            else:
                inputs[2]["connectors"][0]["state"] = "failed"
            output = describe(inputs)
            self.assertEqual(output["status"], "blocked")
            self.assertEqual(output["registration_status"], "planned")
            self.assertEqual(output["proposal"]["steps"], [])
            self.assertIs(output["proposal"]["steps_withheld"], True)
            self.assertIs(output["proposal"]["writes_available"], False)

    def test_generation_freshness_and_binding_are_checked(self):
        """違う世代または別契約の報告を流用させない。"""
        for key, value, code in (
            (
                "environment",
                "elsewhere",
                "RUNTIME_SCOPE_OR_GENERATION_MISMATCH",
            ),
            ("generation", "1" * 64, "RUNTIME_SCOPE_OR_GENERATION_MISMATCH"),
            ("registration_sha256", "0" * 64, "RUNTIME_VIEW_MISMATCH"),
            ("desired_spec_sha256", "0" * 64, "RUNTIME_VIEW_MISMATCH"),
            (
                "observed_at",
                NOW - 86_401,
                "RUNTIME_OBSERVATION_STALE_OR_FUTURE",
            ),
            ("observed_at", NOW + 1, "RUNTIME_OBSERVATION_STALE_OR_FUTURE"),
        ):
            inputs = fixture()
            inputs[2][key] = value
            with self.subTest(field=key):
                with self.assertRaisesRegex(ValueError, code):
                    describe(inputs)

    def test_partition_count_duplicate_ranges_and_scalar_are_strict(self):
        """不足・重複・不連続位置やboolをoffsetとして受け付けない。"""
        for count, change, code in (
            (2, {}, "INVALID_OBSERVED_LIST"),
            (1, {"partition": 1}, "PARTITION_OUTSIDE_OBSERVED_RANGE"),
            (1, {"broker_next": True}, "INVALID_OBSERVED_QUANTITY"),
        ):
            inputs = fixture()
            row = inputs[2]["tables"][0]
            row["partition_count"] = count
            row["partitions"][0].update(change)
            with self.assertRaisesRegex(ValueError, code):
                describe(inputs)
        inputs = fixture()
        row = inputs[2]["tables"][0]
        row["partition_count"] = 2
        row["partitions"] *= 2
        with self.assertRaisesRegex(
            ValueError, "INVALID_OR_DUPLICATE_PARTITION"
        ):
            describe(inputs)

    def test_cli_masks_identifiers_and_raw_errors(self):
        """コマンドを実行してもファイル、fixture、本文は出ない。"""
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp)
            inputs = fixture()
            paths = [top / name for name in ("one", "two", "three")]
            cmd = [
                sys.executable,
                "-S",
                CLI,
                "--offline-fixture",
                "--fixture-id",
                FIXTURE,
                "--generation",
                GENERATION,
                "--as-of-epoch",
                str(NOW),
            ]
            for flag, path in zip(
                ("desired", "registration", "runtime"), paths
            ):
                cmd.extend(["--" + flag, path])

            def check(code):
                for path, document in zip(paths, inputs):
                    path.write_text(json.dumps(document), encoding="utf-8")
                found = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=5, check=False
                )
                self.assertEqual(found.returncode, code)
                self.assertEqual(found.stderr, "")
                for item in (
                    str(top),
                    FIXTURE,
                    GENERATION,
                    "internal_shape_marker",
                    "arbitrary_secret_content",
                ):
                    self.assertNotIn(item, found.stdout)
                return json.loads(found.stdout)

            self.assertEqual(check(0)["status"], "observed_aligned")
            inputs[2]["tables"][0]["last_error"] = "arbitrary_secret_content"
            self.assertEqual(check(2)["code"], "INVALID_OBSERVED_ENUM")


if __name__ == "__main__":
    unittest.main()
