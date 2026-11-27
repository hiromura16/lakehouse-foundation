"""合成 snapshot の読取り統合を、実サービスに接続せず検証する。"""

import copy
import importlib
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cdc_contract import table as make_table
from test_cdc_contract_observation import GENERATION, NOW, fixture
from test_cdc_contract_registration import FIXTURE, observed

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/cdc_collect_fixture.py"
with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
    model = importlib.import_module("cdc_collect_fixture")


def sample():
    """別取得面から得たと仮定する最小の合成証跡を作る。"""
    desired, registration, expected = fixture()
    connector = model.compile_spec(desired)["connectors"][0]
    route = connector["routes"][0]
    table = expected["tables"][0]
    connector_id = model.digest(
        [connector["database.names"], connector["topic.prefix"]]
    )
    route_hash = model.digest(route)
    rows = {
        "sql": dict(
            binding_sha256=route_hash,
            source_sha256=model.digest(route["source"]),
            columns=copy.deepcopy(list(reversed(route["columns"]))),
            primary_key=list(route["primary_key"]),
            **{
                name: table["sql"][name]
                for name in (
                    "table_exists",
                    "database_cdc_enabled",
                    "table_cdc_enabled",
                    "capture_visible",
                    "capture_count",
                )
            },
        ),
        "connect": {
            "binding_sha256": connector_id,
            "configuration": {
                key: connector[key] for key in model.CONFIGURATION_FIELDS
            },
            "observed_spec_sha256": model.digest(connector),
            "state": "running",
            "tasks": ["running"],
            "snapshot": "complete",
        },
        "broker": {
            "binding_sha256": route_hash,
            "group_sha256": table["group_sha256"],
            "active_topic": route["topics"]["canonical"],
            "partition_count": 1,
            "partitions": [
                {
                    k: v
                    for k, v in table["partitions"][0].items()
                    if k != "local_next"
                }
            ],
        },
        "consumer": {
            "binding_sha256": route_hash,
            "group_sha256": table["group_sha256"],
            "topic_sha256": table["topic_sha256"],
            "last_event_age_seconds": table["last_event_age_seconds"],
            "last_error": table["last_error"],
            "checkpoints": [{"partition": 0, "local_next": 11}],
        },
        "iceberg": {"binding_sha256": route_hash, "targets": table["targets"]},
    }
    view = model.assessment(desired, registration, FIXTURE)
    receipts = {
        domain: {
            "version": 1,
            "domain": domain,
            "generation": GENERATION,
            "environment": "synthetic",
            "fixture_id": FIXTURE,
            "observed_at": NOW - 1,
            "registration_sha256": view["observation_sha256"],
            "desired_contract_sha256": view["desired_contract_sha256"],
            "rows": [row],
        }
        for domain, row in rows.items()
    }
    return desired, registration, receipts, expected


def evaluate(*values):
    """合成scopeで既存3Bへ繋ぐ実際の入口。"""
    return model.collect(*values, FIXTURE, GENERATION, NOW)


class CollectorTests(unittest.TestCase):
    """未観測と照合済みを分け、別環境の情報を加工して採用しない。"""

    def test_five_source_join_is_exact_3b_input(self):
        """source列順の違いだけは正規化し、V1の既入力と等しくする。"""
        desired, reg, receipts, expected = sample()
        original = copy.deepcopy(receipts)
        runtime, summary = evaluate(desired, reg, receipts)
        self.assertEqual(runtime, expected)
        self.assertEqual(summary["status"], "observed_aligned")
        self.assertEqual(receipts, original)
        receipts["iceberg"]["rows"][0]["targets"]["raw"]["row_count"] = 0
        self.assertEqual(
            runtime["tables"][0]["targets"]["raw"]["row_count"], 11
        )
        self.assertEqual(
            summary["tables"][0]["partitions"][0]["broker_lag"], 4
        )

    def test_scope_window_cross_mix_and_arbitrary_endpoints_fail_closed(self):
        """別世代、時間窓外、未定義エンドポイントは入力にできない。"""
        for key, value, expected in (
            ("generation", "0" * 64, "COLLECTOR_SCOPE_MISMATCH"),
            ("environment", "production", "COLLECTOR_SCOPE_MISMATCH"),
            ("observed_at", NOW - 32, "COLLECTOR_MIXED_OBSERVATION_INTERVALS"),
            ("observed_at", NOW + 1, "COLLECTOR_OBSERVATION_OUTSIDE_WINDOW"),
            ("endpoint", "private_marker", "COLLECTOR_ENVELOPE"),
        ):
            desired, reg, receipts, _ = sample()
            receipts["broker"][key] = value
            with self.assertRaisesRegex(ValueError, expected):
                evaluate(desired, reg, receipts)

    def test_each_receipt_attests_registry_and_full_desired_view(self):
        """同じactive routeでも別時点の登録・無効表差分を混ぜない。"""
        desired, reg, receipts, _ = sample()
        with self.assertRaisesRegex(
            ValueError, "COLLECTOR_DECLARED_VIEW_MISMATCH"
        ):
            evaluate(desired, observed(), receipts)
        disabled = make_table("disabled_fixture")
        disabled["enabled"] = False
        new = copy.deepcopy(desired)
        new["tables"].append(disabled)
        self.assertEqual(model.compile_spec(new), model.compile_spec(desired))
        with self.assertRaisesRegex(
            ValueError, "COLLECTOR_DECLARED_VIEW_MISMATCH"
        ):
            evaluate(new, reg, receipts)

    def test_duplicate_missing_incorrect_route_binding_rejected(self):
        """件数や並びだけで別レイヤの値を流用しない。"""
        for op, expected in (
            ("duplicate", "COLLECTOR_ROW_BINDING"),
            ("empty", "COLLECTOR_MISSING_ROWS"),
            ("foreign", "COLLECTOR_ROW_BINDING"),
        ):
            desired, reg, receipts, _ = sample()
            rows = receipts["iceberg"]["rows"]
            if op == "duplicate":
                rows *= 2
            elif op == "empty":
                rows.clear()
            else:
                rows[0]["binding_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, expected):
                evaluate(desired, reg, receipts)

    def test_sql_exact_pk_order_and_type_checked_independently(self):
        """主キー並び・DECIMAL等の型を旗だけで受け入れない。"""
        desired, reg, receipts, _ = sample()
        sql = receipts["sql"]["rows"][0]
        sql["primary_key"] = list(reversed(sql["primary_key"]))
        next(c for c in sql["columns"] if c["type"] == "decimal")["scale"] = 1
        runtime, summary = evaluate(desired, reg, receipts)
        row = runtime["tables"][0]["sql"]
        self.assertIs(row["primary_key_match"], False)
        self.assertIs(row["columns_match"], False)
        self.assertEqual(summary["status"], "blocked")

    def test_never_fills_unobserved_or_copies_foreign_values(self):
        """違うgroup/SQLには元の集計やエラーを関連付けない。"""
        desired, reg, receipts, _ = sample()
        consumer = receipts["consumer"]["rows"][0]
        consumer["group_sha256"] = "0" * 64
        consumer["last_error"] = "storage"
        receipts["sql"]["rows"][0]["source_sha256"] = "0" * 64
        runtime, summary = evaluate(desired, reg, receipts)
        row = runtime["tables"][0]
        self.assertTrue(all(value is None for value in row["sql"].values()))
        self.assertIsNone(row["partitions"][0]["local_next"])
        self.assertIsNone(row["last_error"])
        self.assertIsNone(row["last_event_age_seconds"])
        self.assertEqual(summary["status"], "blocked")

    def test_foreign_or_duplicate_checkpoint_partition_not_merged(self):
        """brokerが観測していないpartitionと重複checkpointを拒否する。"""
        for partition, expected in (
            (1, "COLLECTOR_CHECKPOINT_OUTSIDE_BROKER_PARTITIONS"),
            (0, "COLLECTOR_DUPLICATE_CHECKPOINT_PARTITION"),
        ):
            desired, reg, receipts, _ = sample()
            receipts["consumer"]["rows"][0]["checkpoints"].append(
                {"partition": partition, "local_next": 12}
            )
            with self.assertRaisesRegex(ValueError, expected):
                evaluate(desired, reg, receipts)

    def test_matching_config_never_constructs_missing_consumer_spec_proof(
        self,
    ):
        """五つのConnect許可項目から未観測の全体仕様を合成しない。"""
        desired, reg, receipts, _ = sample()
        state = receipts["connect"]["rows"][0]
        state["observed_spec_sha256"] = None
        runtime, summary = evaluate(desired, reg, receipts)
        self.assertIsNone(runtime["connectors"][0]["spec_sha256"])
        self.assertEqual(summary["status"], "blocked")

    @unittest.skipUnless(hasattr(signal, "setitimer"), "Unix local deadline")
    def test_local_deadline_interrupts_and_restores_signal_handler(self):
        """入力ルートが停止した場合は固定診断で抜け、handlerを戻す。"""
        before = signal.getsignal(signal.SIGALRM)
        with self.assertRaisesRegex(ValueError, "COLLECTOR_LOCAL_TIMEOUT"):
            with model.deadline(0.01):
                signal.pause()
        self.assertIs(signal.getsignal(signal.SIGALRM), before)

    def test_cli_masks_private_lexemes_and_rejects_secret_configuration_keys(
        self,
    ):
        """出力への生証跡や秘密らしい設定値を許可しない。"""
        with tempfile.TemporaryDirectory() as root:
            top = Path(root)
            desired, reg, receipts, _ = sample()

            def call():
                command = [
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
                for flag, body in {
                    "desired": desired,
                    "registration": reg,
                    **receipts,
                }.items():
                    file = top / (flag + ".json")
                    file.write_text(json.dumps(body), encoding="utf-8")
                    command.extend(["--" + flag, file])
                before = {
                    file.name: file.read_bytes() for file in top.iterdir()
                }
                response = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=6,
                    check=False,
                )
                self.assertEqual(response.stderr, "")
                for masked in (
                    str(top),
                    FIXTURE,
                    GENERATION,
                    "internal_shape_marker",
                    "masked_input_value",
                ):
                    self.assertNotIn(masked, response.stdout)
                self.assertEqual(
                    {file.name: file.read_bytes() for file in top.iterdir()},
                    before,
                )
                return response.returncode, json.loads(response.stdout)

            self.assertEqual(call()[1]["status"], "observed_aligned")
            receipts["connect"]["rows"][0]["configuration"]["secret"] = (
                "masked_input_value"
            )
            self.assertEqual(
                call(),
                (
                    2,
                    {
                        "status": "invalid",
                        "code": "COLLECTOR_UNSAFE_CONNECT_CONFIG",
                    },
                ),
            )


if __name__ == "__main__":
    unittest.main()
