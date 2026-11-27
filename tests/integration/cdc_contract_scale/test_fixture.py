"""固定5表・例外拒否・SQL用の正確なfixture計画のテスト。"""

import json
import sqlite3
import unittest
from contextlib import closing
from itertools import islice

from fixture import (
    TABLES,
    Table,
    contract,
    counts,
    deliveries,
    final_rows,
    reconcile,
    snapshot_query,
    sql_step,
    steps,
)


class ScaleTest(unittest.TestCase):
    """負例を含め、合成fixture自体が必要件数を減らさないことを確認。"""

    def test_exact_counts_and_real_contract_demands(self):
        """件数、実契約処理、最終行・履歴replayの一致。"""
        actual = reconcile(deliveries())
        self.assertEqual(
            actual,
            {
                "tables": 5,
                "snapshot": 112,
                "insert": 256,
                "update": 512,
                "delete": 256,
                "tombstone": 256,
            },
        )

    def test_per_topic_and_total_snapshot_distribution(self):
        """表別に同じ固定件数と同じパラメータを再現する。"""
        expected = [(16, 128), (20, 160), (24, 192), (24, 256), (28, 288)]
        total = 0
        for table, (snapshot, changes) in zip(TABLES, expected, strict=True):
            events = list(steps(table))
            self.assertEqual(sum(e.op == "r" for e in events), snapshot)
            self.assertEqual(len(events) - snapshot - table.cycles, changes)
            self.assertEqual(len(final_rows(table)), snapshot)
            self.assertEqual(events, list(steps(table)))
            total += len(events)
        self.assertEqual(total, 1392)
        self.assertEqual(counts()["tombstone"], 256)
        self.assertEqual(
            len({next(steps(table)).after["quantity"] for table in TABLES}), 5
        )

    def test_missing_duplicate_and_malformed_delivery_fail_closed(self):
        """既存replayの重複無害化だけで試験を合格扱いにしない。"""
        records = list(deliveries())
        with self.assertRaisesRegex(ValueError, "SCALE_MISSING_DELIVERY"):
            reconcile(iter(records[:-1]))
        with self.assertRaisesRegex(ValueError, "SCALE_TOPIC_POSITION"):
            reconcile(iter([records[0], *records]))
        wrong = dict(records[0], topic="different")
        with self.assertRaisesRegex(ValueError, "SCALE_UNKNOWN_TOPIC"):
            reconcile(iter([wrong, *records[1:]]))
        copied = json.loads(records[0]["value"])
        copied["source"]["table"] = TABLES[1].name
        wrong = dict(records[0], value=json.dumps(copied))
        with self.assertRaisesRegex(ValueError, "SCALE_SOURCE_MISMATCH"):
            reconcile(iter([wrong, *records[1:]]))

    def test_failed_materialization_holds_only_affected_modeled_partition(
        self,
    ):
        """1表の失敗はRawに残し、再open/実Icebergの保証とは区別する。"""
        from contract_experiment import ContractBatchError, Experiment

        first, other = islice(deliveries(), 2)

        def unavailable():
            raise RuntimeError("MODELED_STORAGE_UNAVAILABLE")

        with closing(sqlite3.connect(":memory:")) as state:
            adapter = Experiment(contract(), state)
            with self.assertRaisesRegex(
                ContractBatchError, "DERIVED_STORAGE_FAILED"
            ):
                adapter.process([first], fail_materialization=unavailable)
            self.assertEqual(adapter.raw_count(), 1)
            self.assertEqual(adapter.offset(first["topic"], 0), -1)
            self.assertEqual(adapter.quarantine_count(), 1)
            adapter.process([other])
            self.assertEqual(adapter.offset(other["topic"], 0), 0)
            self.assertEqual(adapter.offset(first["topic"], 0), -1)
            adapter.process([first])
            self.assertEqual(adapter.offset(first["topic"], 0), 0)
            self.assertEqual(adapter.raw_count(), 2)
            with closing(adapter.replay().db) as rebuilt:
                count = rebuilt.execute("SELECT count(*) FROM accepted")
                self.assertEqual(count.fetchone(), (2,))

    def test_tombstone_is_not_sql_delete_and_may_not_be_json_null(self):
        """Kafka nullの配送として数え、文字列のnullやSQLにしない。"""
        table = TABLES[0]
        original = list(steps(table))
        tombstone = next(step for step in original if step.op == "tombstone")
        self.assertIsNone(sql_step(table, tombstone))
        record = next(row for row in deliveries() if row["value"] is None)
        bad = dict(record, value="null")
        prefix = []
        for candidate in deliveries():
            if candidate == record:
                break
            prefix.append(candidate)
        with self.assertRaisesRegex(ValueError, "SCALE_EXPECTED_KAFKA_NULL"):
            reconcile(iter([*prefix, bad]))

    def test_sql_recipe_stays_literal_names_precision_and_deterministic_order(
        self,
    ):
        """固定表識別子と7桁時刻を別々に扱う実行用DMLレシピ。"""
        table = TABLES[0]
        query = snapshot_query(table)
        self.assertTrue(query.endswith("ORDER BY site_id, record_id"))
        with self.assertRaisesRegex(ValueError, "UNKNOWN_SYNTHETIC_TABLE"):
            snapshot_query(Table("anything_else", 16, 32))
        first = list(islice(steps(table), table.initial, table.initial + 5))
        insert, update, delete, without_sql, survivor = [
            sql_step(table, item) for item in first
        ]
        self.assertTrue(
            insert[0].startswith("INSERT INTO [dbo].[fixture_alpha]")
        )
        self.assertEqual(insert[1][0], "synthetic")
        self.assertEqual(insert[1][-1].split(".")[1], "0024690")
        self.assertTrue(update[0].startswith("UPDATE [dbo].[fixture_alpha]"))
        self.assertTrue(
            delete[0].startswith("DELETE FROM [dbo].[fixture_alpha]")
        )
        self.assertIsNone(without_sql)
        self.assertEqual(survivor[1][-1], 1)
