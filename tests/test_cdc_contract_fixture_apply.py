"""合成台帳だけで承認、失敗、続行と監査の境界を検証する。"""

import copy
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_cdc_contract import table
from test_cdc_contract_observation import GENERATION, NOW, fixture
from test_cdc_contract_registration import FIXTURE

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/cdc_fixture_apply.py"
with patch.object(sys, "path", [str(CLI.parent), *sys.path]):
    model = importlib.import_module("cdc_fixture_apply")


class ApplyTests(unittest.TestCase):
    """接続の代わりに専用の一試行・承認の受領書を確定する。"""

    def setUp(self):
        """入力は 3B と同じ合成 fixture。許された新規登録候補だけ。"""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.directory = self.root / FIXTURE
        self.directory.mkdir(mode=0o700)
        self.inputs = fixture(unregistered=True)
        self.plan = model.prepare(*self.inputs, FIXTURE, GENERATION, NOW, 60)
        self.ledger = self.directory / model.LEDGER

    def run_ledger(
        self,
        *,
        operation="apply",
        plan=None,
        confirmation=None,
        inputs=None,
        clock=NOW,
        before_commit=None,
    ):
        """CLIの同じ道筋を固定時計で進め、障害時だけ合成注入する。"""
        if plan is None:
            plan = self.plan
        if confirmation is None:
            confirmation = plan["plan_sha256"]
        return model.execute(
            plan,
            confirmation,
            inputs or self.inputs,
            self.directory,
            FIXTURE,
            GENERATION,
            operation=operation,
            clock=lambda: clock,
            before_commit=before_commit,
        )

    def test_confirmed_plan_is_deterministic_and_idempotent(self):
        """同じ受領書が返り、再適用はファイルさえ変更しない。"""
        self.assertEqual(
            self.plan,
            model.prepare(*self.inputs, FIXTURE, GENERATION, NOW, 60),
        )
        applied = self.run_ledger()
        self.assertEqual(applied["status"], "completed")
        self.assertEqual(applied["completed_steps"], len(self.plan["steps"]))
        self.assertEqual(
            [step["step_sha256"] for step in applied["receipts"]],
            [model.digest(step) for step in self.plan["steps"]],
        )
        self.assertEqual(self.ledger.stat().st_mode & 0o777, 0o600)
        before = self.ledger.read_bytes()
        for option in ("apply", "reconcile"):
            same = self.run_ledger(operation=option)
            self.assertEqual(same["status"], "already_complete")
            self.assertEqual(applied["receipts"], same["receipts"])
            self.assertEqual(self.ledger.read_bytes(), before)

    def test_cannot_create_from_reconcile(self):
        """「再開」で未着手の台帳を作らない。"""
        with self.assertRaisesRegex(ValueError, "FIXTURE_LEDGER_NOT_STARTED"):
            self.run_ledger(operation="reconcile")
        self.assertFalse(self.ledger.exists())

    def test_binding_confirmation_expiry_before_any_writes(self):
        """時刻・間違った承認・混入やドリフトを無変更で止める。"""
        changed = copy.deepcopy(self.inputs)
        changed[2]["tables"][0]["last_event_age_seconds"] += 1
        forged = copy.deepcopy(self.plan)
        forged["steps"][0]["op"] = "UNAPPROVED"
        swapped = dict(self.plan, version=True)
        for options, code in (
            ({"confirmation": "0" * 64}, "EXACT_CONFIRMATION_REQUIRED"),
            ({"clock": NOW + 60}, "PLAN_EXPIRED_OR_FUTURE"),
            ({"clock": NOW - 1}, "PLAN_EXPIRED_OR_FUTURE"),
            ({"inputs": changed}, "APPROVED_PLAN_DRIFT"),
            ({"plan": forged}, "APPROVED_PLAN_DRIFT"),
            ({"plan": swapped}, "INVALID_APPROVAL_FIELDS"),
            (
                {"plan": dict(self.plan, steps=[{"op": []}])},
                "INVALID_APPROVAL_STEPS",
            ),
        ):
            with self.assertRaisesRegex(ValueError, code):
                self.run_ledger(**options)
            self.assertEqual(list(self.directory.iterdir()), [])

    def test_full_desired_contract_bound_even_for_disabled_table(self):
        """active仕様が同じでも、無効表の定義変更は別の承認にする。"""
        inputs = copy.deepcopy(self.inputs)
        disabled = table("disabled_fixture")
        disabled["enabled"] = False
        inputs[0]["tables"].append(disabled)
        plan = model.prepare(*inputs, FIXTURE, GENERATION, NOW, 60)
        changed = copy.deepcopy(inputs)
        changed[0]["tables"][1]["columns"][2]["width"] += 1
        updated = model.prepare(*changed, FIXTURE, GENERATION, NOW, 60)
        self.assertEqual(plan["desired_sha256"], updated["desired_sha256"])
        self.assertNotEqual(
            plan["desired_contract_sha256"], updated["desired_contract_sha256"]
        )
        self.assertNotEqual(plan["plan_sha256"], updated["plan_sha256"])
        with self.assertRaisesRegex(ValueError, "APPROVED_PLAN_DRIFT"):
            self.run_ledger(plan=plan, inputs=changed)
        self.assertFalse(self.ledger.exists())

    def test_obs_expired_by_apply_time_even_with_valid_plan_expiry(self):
        """発行時は有効でも、適用時に古い証跡を再利用しない。"""
        inputs = copy.deepcopy(self.inputs)
        inputs[2]["observed_at"] = NOW - 86_400
        plan = model.prepare(*inputs, FIXTURE, GENERATION, NOW, 60)
        with self.assertRaisesRegex(
            ValueError, "RUNTIME_OBSERVATION_STALE_OR_FUTURE"
        ):
            self.run_ledger(plan=plan, inputs=inputs, clock=NOW + 1)
        self.assertFalse(self.ledger.exists())

    def test_partial_rolls_back_failed_step_and_resumes_after_reopen(self):
        """未確定段階だけ戻し、別接続の再開で既確定順序を維持する。"""

        def interrupt(number):
            if number == 3:
                raise model.FixtureInterrupted("irrelevant detail")

        partial = self.run_ledger(before_commit=interrupt)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["completed_steps"], 2)
        with closing(sqlite3.connect(self.ledger)) as reader:
            self.assertEqual(
                reader.execute(
                    "SELECT ordinal FROM receipts ORDER BY 1"
                ).fetchall(),
                [(1,), (2,)],
            )
            self.assertEqual(
                reader.execute("SELECT applied_sha FROM control").fetchone(),
                (None,),
            )
        with self.assertRaisesRegex(ValueError, "EXPLICIT_RECONCILE_REQUIRED"):
            self.run_ledger()
        with self.assertRaisesRegex(ValueError, "PLAN_EXPIRED_OR_FUTURE"):
            self.run_ledger(operation="reconcile", clock=NOW + 60)
        resumed = self.run_ledger(operation="reconcile")
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["receipts"][:2], partial["receipts"])
        self.assertEqual(
            [item["ordinal"] for item in resumed["receipts"]],
            list(range(1, len(self.plan["steps"]) + 1)),
        )
        with closing(sqlite3.connect(self.ledger)) as reader:
            self.assertEqual(
                reader.execute("SELECT applied_sha FROM control").fetchone(),
                (self.plan["desired_sha256"],),
            )

    def test_expiry_inside_a_step_does_not_commit_that_receipt(self):
        """開始時は有効でもcommit前に期限切れした段階は戻す。"""
        clock = [NOW]

        def advance(number):
            if number == 2:
                clock[0] = NOW + 60

        with self.assertRaisesRegex(ValueError, "PLAN_EXPIRED_OR_FUTURE"):
            model.execute(
                self.plan,
                self.plan["plan_sha256"],
                self.inputs,
                self.directory,
                FIXTURE,
                GENERATION,
                operation="apply",
                clock=lambda: clock[0],
                before_commit=advance,
            )
        with closing(sqlite3.connect(self.ledger)) as reader:
            self.assertEqual(
                reader.execute("SELECT ordinal FROM receipts").fetchall(),
                [(1,)],
            )

    def test_busy_or_linked_fixture_does_not_adopt_another_file(self):
        """同時更新は失敗し、既存ハードリンクも触らない。"""
        with model.locked_fixture(self.directory, FIXTURE):
            with self.assertRaisesRegex(ValueError, "FIXTURE_LEDGER_BUSY"):
                self.run_ledger()
        self.assertFalse(self.ledger.exists())
        unrelated = self.root / "synthetic_unrelated"
        unrelated.write_bytes(b"not ledger")
        unrelated.chmod(0o600)
        self.ledger.hardlink_to(unrelated)
        with self.assertRaisesRegex(ValueError, "UNSAFE_LEDGER_FILE"):
            self.run_ledger()
        self.assertEqual(unrelated.read_bytes(), b"not ledger")

    def test_refuses_foreign_database_and_unowned_directory(self):
        """同名の別DB、他ファイル、権限の広い場所は取り込まない。"""
        with closing(sqlite3.connect(self.ledger)) as other:
            with other:
                other.execute("CREATE TABLE unrelated (marker INTEGER)")
        self.ledger.chmod(0o600)
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "LEDGER_HEADER_MISMATCH"):
            self.run_ledger()
        self.assertEqual(self.ledger.read_bytes(), before)
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "UNSAFE_LEDGER_DIRECTORY"):
            self.run_ledger()
        self.directory.chmod(0o700)
        marker = self.directory / "unrelated"
        marker.touch()
        with self.assertRaisesRegex(
            ValueError, "FIXTURE_DIRECTORY_NOT_DEDICATED"
        ):
            self.run_ledger()
        self.assertTrue(marker.exists())

    def test_cyclic_directory_is_a_fixed_diagnostic(self):
        """解決不能なlinkのパスを例外出力に載せない。"""
        self.directory.rmdir()
        self.directory.symlink_to(self.directory.name)
        with self.assertRaisesRegex(ValueError, "^UNSAFE_LEDGER_DIRECTORY$"):
            self.run_ledger()

    def test_hot_journal_and_receipt_tamper_do_not_autorepair(self):
        """未知の回復ログや改変したチェックポイントは無変更で止まる。"""
        self.run_ledger()
        journal = self.directory / (model.LEDGER + "-journal")
        journal.write_bytes(b"unrelated synthetic marker")
        with self.assertRaisesRegex(ValueError, "HOT_JOURNAL_UNSUPPORTED"):
            self.run_ledger(operation="reconcile")
        self.assertEqual(journal.read_bytes(), b"unrelated synthetic marker")
        journal.unlink()
        with closing(sqlite3.connect(self.ledger)) as writer:
            with writer:
                writer.execute("UPDATE receipts SET step_sha='invalid'")
        before = self.ledger.read_bytes()
        with self.assertRaisesRegex(ValueError, "LEDGER_RECEIPT_MISMATCH"):
            self.run_ledger(operation="reconcile")
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_changed_plan_cannot_adopt_existing_ledger(self):
        """同fixtureでも異なる承認は既存台帳と結び付かない。"""
        self.run_ledger()
        second = model.prepare(*self.inputs, FIXTURE, GENERATION, NOW, 120)
        with self.assertRaisesRegex(
            ValueError, "LEDGER_PLAN_BINDING_MISMATCH"
        ):
            self.run_ledger(plan=second)

    def test_cli_executes_only_named_fixture_and_never_prints_paths(self):
        """prepare→明示確認→applyを実プロセスで通し、値を出さない。"""
        inputs = copy.deepcopy(self.inputs)
        inputs[2]["observed_at"] = int(time.time()) - 1
        flags = [
            "--offline-fixture",
            "--fixture-id",
            FIXTURE,
            "--generation",
            GENERATION,
        ]
        for name, doc in zip(("desired", "registration", "runtime"), inputs):
            location = self.root / name
            location.write_text(json.dumps(doc), encoding="utf-8")
            flags.extend(["--" + name, location])

        def call(operation, *extra):
            actual = subprocess.run(
                [sys.executable, "-S", CLI, operation, *flags, *extra],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(actual.stderr, "")
            for private in (
                FIXTURE,
                GENERATION,
                os.fspath(self.root),
                "internal_shape_marker",
            ):
                self.assertNotIn(private, actual.stdout)
            return actual.returncode, json.loads(actual.stdout)

        status, plan = call("prepare")
        self.assertEqual(status, 0)
        self.assertFalse(self.ledger.exists())
        save = self.root / "approved.json"
        save.write_text(json.dumps(plan), encoding="utf-8")
        common = (
            "--ledger-dir",
            self.directory,
            "--plan",
            save,
            "--confirm-sha256",
        )
        self.assertEqual(call("apply", *common, "0" * 64)[0], 2)
        self.assertFalse(self.ledger.exists())
        code, applied = call("apply", *common, plan["plan_sha256"])
        self.assertEqual(code, 0)
        self.assertEqual(applied["status"], "completed")


if __name__ == "__main__":
    unittest.main()
