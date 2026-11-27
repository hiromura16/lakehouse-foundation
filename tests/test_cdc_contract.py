"""公開向け CDC 検査 CLI を合成 JSON だけで検証する。"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/cdc_contract.py"


def table(identifier="products"):
    """外部への参照を持たない、複合キーと複数型の小さい宣言。"""
    return {
        "id": identifier,
        "enabled": True,
        "source": {
            "database": "fixture_db",
            "schema": "fixture",
            "table": identifier,
            "topic_prefix": "fixture-cdc",
        },
        "primary_key": ["batch_id", "row_id"],
        "mode": "current",
        "destinations": {
            "raw": {"namespace": "raw", "table": "events", "stream": "family"},
            "current": {"namespace": "current", "table": identifier},
            "silver": {"namespace": "silver", "table": identifier},
        },
        "columns": [
            {"name": "batch_id", "type": "bigint", "nullable": False},
            {"name": "row_id", "type": "char", "width": 12, "nullable": False},
            {
                "name": "name",
                "type": "nvarchar",
                "width": 40,
                "nullable": True,
            },
            {
                "name": "qty",
                "type": "decimal",
                "nullable": True,
                "precision": 10,
                "scale": 2,
            },
            {
                "name": "stamp",
                "type": "datetime2",
                "scale": 3,
                "nullable": True,
            },
        ],
        "delete_policy": "remove",
        "snapshot": "initial",
    }


class CliTests(unittest.TestCase):
    """実 CLI の終了値・固定出力・意味上の制約を検査する。"""

    def call(self, mode, *documents):
        """標準ライブラリだけで実行し、識別子やファイルパスを漏らさない。"""
        with tempfile.TemporaryDirectory(
            prefix="cdc-contract-fixture-"
        ) as tmp:
            paths = []
            for index, document in enumerate(documents):
                path = Path(tmp) / f"private-sentinel-{index}.json"
                path.write_text(
                    document
                    if isinstance(document, str)
                    else json.dumps(document),
                    encoding="utf-8",
                )
                paths.append(path)
            run = subprocess.run(
                [sys.executable, "-S", CLI, mode, *paths],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(run.stderr, "")
        self.assertNotIn("private-sentinel", run.stdout)
        return run.returncode, json.loads(run.stdout)

    def rejection(self, document, expected):
        """拒否結果が定型コードだけであることを確認する。"""
        self.assertEqual(
            self.call("validate", document),
            (1, {"status": "invalid", "code": expected}),
        )

    def test_same_raw_stream_shared_only_with_exact_destination(self):
        """明示共有だけを認め、Current/Silver/Raw の衝突を拒否する。"""
        rows = [table(), table("orders")]
        doc = {"version": 1, "tables": rows}
        self.assertEqual(
            self.call("validate", doc),
            (0, {"status": "valid", "table_count": 2}),
        )
        for kind, override, code in (
            ("raw", {"stream": "unrelated"}, "DUPLICATE_DESTINATION"),
            ("raw", {"table": "other"}, "RAW_STREAM_DESTINATION_CHANGED"),
            ("current", {"namespace": "silver"}, "DUPLICATE_DESTINATION"),
            (
                "silver",
                {"namespace": "raw", "table": "events"},
                "DUPLICATE_DESTINATION",
            ),
        ):
            candidate = copy.deepcopy(doc)
            candidate["tables"][1]["destinations"][kind].update(override)
            self.rejection(candidate, code)

    def test_physical_source_and_short_topic_alias_are_disjoint(self):
        """ID や prefix の変更で同じ物理表を二重登録できない。"""
        first, second = table(), table("orders")
        second["source"] = dict(first["source"], topic_prefix="other")
        doc = {"version": 1, "tables": [first, second]}
        self.rejection(doc, "DUPLICATE_PHYSICAL_SOURCE")
        # DB付き topic は別でも、DB無し topic は同じになる。
        second["source"] = dict(first["source"], database="other_db")
        self.rejection(doc, "DUPLICATE_TOPIC_ALIAS")
        second["source"]["topic_prefix"] = "other"
        self.assertEqual(self.call("validate", doc)[0], 0)

    def test_system_databases_are_rejected(self):
        """予約 DB をアプリケーションの取得元として通さない。"""
        for name in ("master", "model", "msdb", "tempdb"):
            candidate = table()
            candidate["source"]["database"] = name
            self.rejection(
                {"version": 1, "tables": [candidate]}, "SYSTEM_DATABASE"
            )

    def test_assembled_topic_length_bound_includes_dots(self):
        """DB付き別名はASCII249文字まで、250文字では固定コードを返す。"""
        row = table()
        source = row["source"]
        source.update(
            database="a" * 62,
            schema="b" * 62,
            table="c" * 62,
            topic_prefix="d" * 60,
        )
        doc = {"version": 1, "tables": [row]}
        self.assertEqual(self.call("validate", doc)[0], 0)
        source["topic_prefix"] += "d"
        self.rejection(doc, "TOPIC_ALIAS_TOO_LONG")

    def test_primary_key_member_limit(self):
        """SQL Server の複合キーは32列までで、33列は拒否する。"""
        row = table()
        row["columns"] = [
            {"name": f"key_{i}", "type": "bigint", "nullable": False}
            for i in range(33)
        ]
        row["primary_key"] = [col["name"] for col in row["columns"][:32]]
        doc = {"version": 1, "tables": [row]}
        self.assertEqual(self.call("validate", doc)[0], 0)
        row["primary_key"].append("key_32")
        self.rejection(doc, "INVALID_PRIMARY_KEY")

    def test_lob_is_allowed_for_values_but_not_primary_key(self):
        """同じmax幅でも、値専用と主キーを区別する。"""
        for kind in ("nvarchar", "varchar"):
            row = table()
            row["columns"][2].update(type=kind, width="max")
            doc = {"version": 1, "tables": [row]}
            self.assertEqual(self.call("validate", doc)[0], 0)
            row["columns"][2]["nullable"] = False
            row["primary_key"][1] = "name"
            self.rejection(doc, "LOB_PRIMARY_KEY_COLUMN")

    def test_key_column_definition_changes_rejected_even_when_widened(self):
        """文字・小数・日時のキー表現は、値域が広がっても変更しない。"""
        for kind, prior, wider in (
            ("char", {"width": 12}, {"width": 24}),
            (
                "decimal",
                {"precision": 10, "scale": 2},
                {"precision": 12, "scale": 4},
            ),
            ("datetime2", {"scale": 3}, {"scale": 7}),
        ):
            row = table()
            row["columns"][1] = {
                "name": "row_id",
                "type": kind,
                "nullable": False,
                **prior,
            }
            old = {"version": 1, "tables": [row]}
            new = copy.deepcopy(old)
            new["tables"][0]["columns"][1].update(wider)
            self.assertEqual(
                self.call("diff", old, new),
                (
                    1,
                    {
                        "status": "incompatible",
                        "issues": [
                            {
                                "code": "PRIMARY_KEY_COLUMN_CHANGED",
                                "table": 1,
                                "column": 2,
                            }
                        ],
                    },
                ),
            )

    def test_cli_misuse_has_fixed_output_and_help_still_works(self):
        """誤った argv を出力に転記せず、通常のヘルプを提供する。"""
        for args in (
            ("private-sentinel",),
            ("validate", "--private-sentinel"),
        ):
            run = subprocess.run(
                [sys.executable, "-S", CLI, *args],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(run.returncode, 2)
            self.assertEqual(run.stderr, "")
            self.assertEqual(
                json.loads(run.stdout),
                {"status": "invalid", "code": "INVALID_USAGE"},
            )
        run = subprocess.run(
            [sys.executable, "-S", CLI, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(run.returncode, 0)
        self.assertIn("validate,diff", run.stdout)

    def test_duplicate_and_nonfinite_json_are_not_accepted(self):
        """読み取り時点で重複キー・非標準 JSON を拒否する。"""
        self.rejection('{"version":1,"version":1}', "DUPLICATE_JSON_KEY")
        self.rejection('{"version":NaN}', "INVALID_JSON")

    def test_invalid_identity_and_unknown_fields_are_not_echoed(self):
        """接続情報や想定外属性を受理・表示しない。"""
        doc = {"version": 1, "tables": [table()]}
        doc["tables"][0]["source"]["database"] = "private-sentinel.invalid"
        self.rejection(doc, "INVALID_SOURCE_IDENTITY")
        doc["tables"][0]["source"]["host"] = "private-sentinel.invalid"
        self.rejection(doc, "INVALID_SOURCE_FIELDS")

    def test_strict_types_key_policy_and_column_shapes(self):
        """boolと整数の混同、NULLキーや不成立な精度を防ぐ。"""
        for case, expected in (
            ("version", "UNSUPPORTED_CONTRACT_VERSION"),
            ("enabled", "INVALID_TABLE_IDENTITY"),
            ("duplicate", "DUPLICATE_COLUMN"),
            ("key", "INVALID_PRIMARY_KEY_COLUMN"),
            ("width", "INVALID_COLUMN_WIDTH"),
            ("decimal", "INVALID_DECIMAL_SHAPE"),
            ("policy", "INVALID_DELETE_POLICY"),
        ):
            row = table()
            doc = {"version": 1, "tables": [row]}
            if case == "version":
                doc["version"] = True
            elif case == "enabled":
                row["enabled"] = 1
            elif case == "duplicate":
                row["columns"].append(row["columns"][0])
            elif case == "key":
                row["primary_key"] = ["name"]
            elif case == "width":
                row["columns"][1]["width"] = True
            elif case == "decimal":
                row["columns"][3]["scale"] = 11
            elif case == "policy":
                row["mode"] = "append"
            self.rejection(doc, expected)

    def test_nullable_add_and_value_domain_widen_are_compatible(self):
        """文字幅と小数・時刻精度の拡張は値域を保持する。"""
        old = {"version": 1, "tables": [table()]}
        new = copy.deepcopy(old)
        cols = new["tables"][0]["columns"]
        cols[2]["width"] = "max"
        cols[3].update(precision=12, scale=4)
        cols[4]["scale"] = 7
        cols.append({"name": "added", "type": "date", "nullable": True})
        self.assertEqual(
            self.call("diff", old, new),
            (0, {"status": "compatible", "issues": []}),
        )

    def test_diff_detects_removal_narrowing_and_ordered_key_changes(self):
        """小数の整数部減少と列削除を、順序変更と別の理由で示す。"""
        old = {"version": 1, "tables": [table()]}
        new = copy.deepcopy(old)
        row = new["tables"][0]
        row["primary_key"].reverse()
        row["columns"].pop(2)
        row["columns"][2]["scale"] = 3
        row["columns"].append(
            {"name": "added", "type": "date", "nullable": False}
        )
        status, verdict = self.call("diff", old, new)
        self.assertEqual(status, 1)
        self.assertEqual(verdict["status"], "incompatible")
        self.assertEqual(
            [issue["code"] for issue in verdict["issues"]],
            [
                "PRIMARY_KEY_CHANGED",
                "COLUMN_REMOVED",
                "COLUMN_NARROWED_OR_CHANGED",
                "REQUIRED_COLUMN_ADDED",
            ],
        )
