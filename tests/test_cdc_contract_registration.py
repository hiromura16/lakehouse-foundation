"""CDC 登録の読み取り面を合成ファイルだけで検証する。"""

import copy
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_cdc_contract import table

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/cdc_registration.py"
with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
    registration = importlib.import_module("cdc_registration")

FIXTURE = "fixture_" + "a" * 32
OTHER = "fixture_" + "b" * 32


def desired(*tables):
    """表示名や接続先を含まない合成契約を作る。"""
    return {"version": 1, "tables": list(tables)}


def observed(previous=None, *, state=None, fixture=FIXTURE):
    """許可された観測 envelope を固定する。"""
    if state is None:
        state = "present" if previous else "absent"
    return {
        "version": 1,
        "environment": "synthetic",
        "fixture_id": fixture,
        "state": state,
        "registered_contract": previous,
        "observed_spec_sha256": (
            registration.digest(registration.compile_spec(previous))
            if previous
            else None
        ),
    }


class CompileTests(unittest.TestCase):
    """具体的なフィルタとKafka alias、複合キーを検証する。"""

    def test_exact_allowed_tables_columns_and_topic(self):
        """無効表を漏らさず、ドット置換や複合キー並べ替えを防ぐ。"""
        first, disabled = table(), table("skip_this")
        disabled["enabled"] = False
        first["primary_key"].reverse()
        connectors = registration.compile_spec(desired(disabled, first))[
            "connectors"
        ]
        self.assertEqual(len(connectors), 1)
        connector = connectors[0]
        self.assertEqual(connector["table.include.list"], r"fixture\.products")
        filters = connector["column.include.list"].split(",")
        self.assertEqual(
            filters,
            [
                rf"fixture\.products\.{name}"
                for name in ("batch_id", "name", "qty", "row_id", "stamp")
            ],
        )
        self.assertFalse(re.fullmatch(filters[0], "fixtureXproducts.batch_id"))
        route = connector["routes"][0]
        self.assertEqual(route["primary_key"], ["row_id", "batch_id"])
        self.assertEqual(
            route["topics"],
            {
                "canonical": "fixture-cdc.fixture_db.fixture.products",
                "legacy_alias": "fixture-cdc.fixture.products",
            },
        )
        self.assertEqual(route["destinations"], first["destinations"])
        self.assertEqual(
            next(c for c in route["columns"] if c["name"] == "qty")["scale"],
            2,
        )

    def test_releases_same_intent_in_canonical_order(self):
        """表と列の並びだけでは承認材料も集約仕様も変えない。"""
        original = desired(table(), table("orders"))
        rearranged = copy.deepcopy(original)
        rearranged["tables"].reverse()
        for item in rearranged["tables"]:
            item["columns"].reverse()
        self.assertEqual(
            registration.compile_spec(original),
            registration.compile_spec(rearranged),
        )
        self.assertEqual(
            registration.assessment(original, observed(), FIXTURE),
            registration.assessment(rearranged, observed(), FIXTURE),
        )

    def test_rejects_connector_policy_and_prefix_conflicts(self):
        """一コネクタで混在するsnapshotや履歴prefix衝突を推測しない。"""
        other = table("orders")
        other["snapshot"] = "never"
        with self.assertRaisesRegex(ValueError, "MIXED_SNAPSHOT_POLICY"):
            registration.compile_spec(desired(table(), other))
        other["source"]["database"] = "other_fixture"
        with self.assertRaisesRegex(
            ValueError, "TOPIC_PREFIX_REUSED_ACROSS_DATABASES"
        ):
            registration.compile_spec(desired(table(), other))


class AssessmentTests(unittest.TestCase):
    """環境が違う、未観測、外部ドリフト、非互換を前へ進めない。"""

    def test_append_only_order_and_binding(self):
        """操作順・scope・観測と欲しい仕様を照合値に結び付ける。"""
        document = desired(table("b_table"), table("a_table"))
        result = registration.assessment(document, observed(), FIXTURE)
        self.assertEqual(result["status"], "planned")
        self.assertEqual(
            result["steps"],
            [
                {"op": "RECHECK_SCOPE_AND_OBSERVATION"},
                {"op": "PREFLIGHT_SOURCE_AND_CDC_READ_ONLY"},
                {"op": "REGISTER_TABLE", "table": 1},
                {"op": "REGISTER_TABLE", "table": 2},
                {"op": "VERIFY_TOPICS_AND_DESTINATIONS"},
                {"op": "VERIFY_RETAINED_OFFSETS_AND_RECORD_RECEIPT"},
            ],
        )
        token = result["approval_candidate_sha256"]
        self.assertRegex(token, r"^[0-9a-f]{64}$")
        second = registration.assessment(
            document, observed(fixture=OTHER), OTHER
        )
        self.assertNotEqual(token, second["approval_candidate_sha256"])
        incremental = registration.assessment(
            document, observed(desired(table("a_table"))), FIXTURE
        )
        self.assertNotEqual(token, incremental["approval_candidate_sha256"])
        self.assertEqual(
            incremental["steps"][2:4],
            [
                {"op": "REVIEW_BACKFILL_AND_OFFSETS", "table": 2},
                {"op": "REGISTER_TABLE", "table": 2},
            ],
        )

    def test_noop_and_drift_unknown_do_not_issue_fingerprints(self):
        """一致はnoop、未観測と仕様不一致はblockedで承認候補を出さない。"""
        previous = desired(table())
        known = observed(previous)
        drifted = dict(known, observed_spec_sha256="0" * 64)
        for snapshot, status, blockers in (
            (known, "aligned", []),
            (drifted, "blocked", ["OBSERVED_SPEC_DRIFT"]),
            (observed(state="unknown"), "blocked", ["OBSERVATION_UNKNOWN"]),
        ):
            result = registration.assessment(previous, snapshot, FIXTURE)
            self.assertEqual(result["status"], status)
            self.assertEqual(result["blockers"], blockers)
            self.assertEqual(result["steps"], [])
            self.assertIsNone(result["approval_candidate_sha256"])

    def test_compatible_nullable_addition_not_new_required_or_delete(self):
        """nullable追加は候補、削除・型縮小・無効化は実行候補にしない。"""
        previous = desired(table())
        snapshot = observed(previous)
        newer = copy.deepcopy(previous)
        newer["tables"][0]["columns"].append(
            {"name": "new_optional", "nullable": True, "type": "bit"}
        )
        candidate = registration.assessment(newer, snapshot, FIXTURE)
        self.assertEqual(
            candidate["steps"][2], {"op": "UPDATE_TABLE", "table": 1}
        )
        self.assertNotEqual(
            candidate["desired_spec_sha256"],
            snapshot["observed_spec_sha256"],
        )
        for update in (
            lambda new: new["tables"][0]["columns"].pop(),
            lambda new: new["tables"][0].update(enabled=False),
            lambda new: next(
                c for c in new["tables"][0]["columns"] if "width" in c
            ).update(width=1),
            lambda new: new["tables"][0]["primary_key"].reverse(),
        ):
            incompatible = copy.deepcopy(previous)
            update(incompatible)
            result = registration.assessment(incompatible, snapshot, FIXTURE)
            self.assertEqual(result["status"], "blocked")
            self.assertIn("INCOMPATIBLE_CONTRACT_CHANGE", result["blockers"])
            self.assertIsNone(result["approval_candidate_sha256"])

    def test_snapshot_scope_and_shape_cannot_be_claimed_by_reading_contract(
        self,
    ):
        """他環境、別fixture、未知フィールドと矛盾する観測を拒否する。"""
        item = desired(table())
        for snapshot, reason in (
            (observed(fixture=OTHER), "SYNTHETIC_SCOPE_MISMATCH"),
            (dict(observed(), environment="live"), "SYNTHETIC_SCOPE_MISMATCH"),
            (
                dict(observed(), credential="private"),
                "INVALID_OBSERVATION_FIELDS",
            ),
            (
                dict(observed(), registered_contract=item),
                "INCONSISTENT_OBSERVATION",
            ),
            (dict(observed(), state="present"), "OBSERVATION_INCOMPLETE"),
            (dict(observed(), version=True), "INVALID_OBSERVATION_VERSION"),
        ):
            with self.assertRaisesRegex(ValueError, reason):
                registration.assessment(item, snapshot, FIXTURE)


class CliTests(unittest.TestCase):
    """機密文字列を表示せず、ディスク変更なしで実CLIを呼ぶ。"""

    def invoke(self, where, command="plan", *, opted_in=True):
        """契約と観測ファイルだけを指定し、実プロセスを使う。"""
        args = [
            sys.executable,
            "-S",
            CLI,
            command,
            "--fixture-id",
            FIXTURE,
            "--desired",
            where / "private_fixture.json",
            "--observed",
            where / "private_snapshot.json",
        ]
        if opted_in:
            args.append("--offline-fixture")
        finished = subprocess.run(
            args, capture_output=True, text=True, timeout=5, check=False
        )
        self.assertEqual(finished.stderr, "")
        for private in (
            "private_fixture",
            "private_snapshot",
            "secretlike_identifier",
            FIXTURE,
            str(where),
        ):
            self.assertNotIn(private, finished.stdout)
        return finished.returncode, json.loads(finished.stdout)

    def test_scope_is_required_before_any_file_read(self):
        """明示許可がなければ入力に触れず、書き込みmodeも存在しない。"""
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp)
            self.assertEqual(
                self.invoke(location, opted_in=False),
                (
                    2,
                    {
                        "status": "invalid",
                        "code": "EXPLICIT_SYNTHETIC_SCOPE_REQUIRED",
                    },
                ),
            )
            self.assertEqual(
                self.invoke(location, "apply"),
                (2, {"status": "invalid", "code": "INVALID_USAGE"}),
            )

    def test_report_is_read_only_and_does_not_echo_names(self):
        """statusは差分の診断、planだけに操作と未署名の照合値を出す。"""
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp)
            cfg, snap = (
                location / name
                for name in ("private_fixture.json", "private_snapshot.json")
            )
            cfg.write_text(
                json.dumps(desired(table("secretlike_identifier"))),
                encoding="utf-8",
            )
            snap.write_text(json.dumps(observed()), encoding="utf-8")
            before = [cfg.read_bytes(), snap.read_bytes()]
            code, output = self.invoke(location)
            self.assertEqual(code, 0)
            self.assertEqual(output["status"], "planned")
            self.assertEqual(output["desired_table_count"], 1)
            code, output = self.invoke(location, "status")
            self.assertEqual(code, 0)
            self.assertNotIn("steps", output)
            self.assertNotIn("approval_candidate_sha256", output)
            self.assertEqual(before, [cfg.read_bytes(), snap.read_bytes()])
            self.assertEqual({cfg, snap}, set(location.iterdir()))

    def test_plain_cli_does_not_create_local_bytecode(self):
        """-Bを付けず、キャッシュのないCLIを呼んでもファイルを作らない。"""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            scripts = directory / "scripts"
            scripts.mkdir()
            names = {CLI.name, "cdc_contract.py"}
            for name in names:
                shutil.copyfile(CLI.parent / name, scripts / name)
            env = {
                key: value
                for key, value in os.environ.items()
                if key
                not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}
            }
            command = subprocess.run(
                [sys.executable, "-S", scripts / CLI.name, "--help"],
                env=env,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(command.returncode, 0)
            self.assertEqual({item.name for item in scripts.iterdir()}, names)

    @unittest.skipUnless(
        hasattr(os, "mkfifo"), "Unix fixture-only file guards"
    )
    def test_refuses_fifo_symlink_duplicate_and_overlarge_file(self):
        """通常ファイル以外や、重複とサイズの超過を固定診断で拒否。"""
        with tempfile.TemporaryDirectory() as tmp:
            location = Path(tmp)
            cfg = location / "private_fixture.json"
            os.mkfifo(cfg)
            self.assertEqual(
                self.invoke(location)[1]["code"], "UNSAFE_INPUT_FILE"
            )
            cfg.unlink()
            cfg.symlink_to("private_snapshot.json")
            self.assertEqual(
                self.invoke(location)[1]["code"], "INPUT_FILE_UNREADABLE"
            )
            cfg.unlink()
            cfg.write_text('{"version":1,"version":1}', encoding="utf-8")
            self.assertEqual(
                self.invoke(location)[1]["code"], "DUPLICATE_JSON_KEY"
            )
            cfg.write_bytes(b" " * (registration.MAX_BYTES + 1))
            self.assertEqual(
                self.invoke(location)[1]["code"], "INPUT_FILE_TOO_LARGE"
            )


if __name__ == "__main__":
    unittest.main()
