"""契約consumer自体とSQL由来wire形式をローカルSQLiteだけで突き合わせる。"""

import json
import sqlite3
import unittest
from contextlib import closing
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import proof as src
import wire_iceberg as wire
from test_proof import row


def record(n, operation, before, after):
    """実際のsource形の正確なテスト用wireを作る。"""
    payload = (
        None if operation == "tombstone" else row(operation, before, after)
    )
    if payload is not None:
        payload["source"]["event_serial_no"] = 1
        payload["source"]["commit_lsn"] = f"00000001:00000001:{n:04x}"
        payload["source"]["change_lsn"] = f"00000001:00000002:{n:04x}"
    return {
        "topic": src.TOPIC,
        "partition": 0,
        "offset": n,
        "key": json.dumps(
            {"site_id": "sample", "record_id": 1 if n == 0 else 2}
        ),
        "value": None if payload is None else json.dumps(payload),
        "ingested_at": "2026-01-02T03:04:00.000000Z",
    }


class ContractWireTests(unittest.TestCase):
    """実 Iceberg の境界から切り離し、契約adapterの負例も固定する。"""

    def fixture_deliveries(self):
        """snapshot行と別のI/U/D行が同時に存在するケース。"""
        return (
            record(0, "r", None, src.SNAPSHOT),
            record(1, "c", None, src.INSERTED),
            record(2, "u", src.INSERTED, src.UPDATED),
            record(3, "d", src.UPDATED, None),
            record(4, "tombstone", None, None),
        )

    def test_actual_contract_matches_all_independent_generations(self):
        """実験の契約とhookを使い、予想していない世代/型を許さない。"""
        from contract_experiment import Experiment

        expected = {"version": 1, "tables": [wire.TABLE]}
        deliveries = self.fixture_deliveries()
        with closing(sqlite3.connect(":memory:")) as db:
            experiment = Experiment(expected, db)
            with patch.object(wire, "ensure") as write:
                for sequence, event in enumerate(deliveries):
                    self.assertEqual(
                        wire.apply(experiment, None, event),
                        {
                            "offset": sequence,
                            "key_json": event["key"],
                            "value_json": event["value"],
                        },
                    )
                    self.assertEqual(experiment.offset(src.TOPIC, 0), sequence)
                self.assertEqual(write.call_count, 15)
                self.assertEqual(
                    [call.args[3] for call in write.call_args_list[1::3]],
                    [wire.expected(offset) for offset in range(5)],
                )
                with closing(experiment.replay().db) as rebuilt:
                    self.assertEqual(
                        rebuilt.execute(
                            "SELECT COUNT(*) FROM accepted"
                        ).fetchone(),
                        (5,),
                    )
                self.assertEqual(experiment.quarantine_count(), 0)
                self.assertEqual(experiment.raw_count(), 5)

    def test_handoff_demands_exact_bytes_and_distinguishes_null(self):
        """意味上同じJSONや空のbytesへの置換でも通さない。"""
        prior = (b'{"site_id":"sample"}', None)
        same = SimpleNamespace(key=lambda: prior[0], value=lambda: None)
        wire.same_wire(same, prior)
        for key, value in ((prior[0], b""), (b'{"site_id": "sample"}', None)):
            changed = SimpleNamespace(key=lambda: key, value=lambda: value)
            with self.assertRaisesRegex(src.Failure, "CHAIN_WIRE_CHANGED"):
                wire.same_wire(changed, prior)

    def test_native_snapshot_id_requires_real_positive_integer(self):
        """行が存在するだけでは実Iceberg snapshotを証明しない。"""
        catalog = Mock()
        table = catalog.load_table.return_value
        for invalid in (
            None,
            SimpleNamespace(snapshot_id=0),
            SimpleNamespace(snapshot_id=True),
        ):
            table.current_snapshot.return_value = invalid
            with self.assertRaisesRegex(
                src.Failure, "CHAIN_ICEBERG_SNAPSHOT_ID"
            ):
                wire.snapshot_id(catalog, "raw")
        table.current_snapshot.return_value = SimpleNamespace(snapshot_id=7)
        self.assertEqual(wire.snapshot_id(catalog, "raw"), 7)

    def test_reopen_same_native_ids_and_pins_each_scan(self):
        """元のsnapshotを比較し、現在データの偶然の一致で代用しない。"""
        seen = {"raw": 4, "current": 8, "silver": 12}
        raw = [{"offset": 0, "key_json": "synthetic", "value_json": "{}"}]
        with (
            patch.object(wire, "snapshots", return_value=seen),
            patch.object(
                wire,
                "read",
                side_effect=[raw, wire.expected(0), wire.expected(0)],
            ) as read,
        ):
            wire.reopened_snapshots(None, seen, raw)
            self.assertEqual(
                [call.kwargs for call in read.call_args_list],
                [{"snapshot_id": 4}, {"snapshot_id": 8}, {"snapshot_id": 12}],
            )
        with patch.object(wire, "snapshots", return_value=dict(seen, raw=3)):
            with self.assertRaisesRegex(
                src.Failure, "CHAIN_REOPEN_SNAPSHOT_IDS"
            ):
                wire.reopened_snapshots(None, seen, raw)

    def test_actual_sql_full_typed_row_and_absence_are_separate_gates(self):
        """保持キーしか見ずに通すことや、decimal丸めを許さない。"""
        valid = ("sample", 1, None, Decimal("2.500"), src.STAMP0)
        known = {"current": 3, "silver": 4}
        source = Mock()
        source.execute.side_effect = [[valid], [(0,)]]
        with patch.object(wire, "read", return_value=wire.expected(4)) as read:
            wire.read_final_sql(source, None, known)
        self.assertEqual(
            [c.kwargs["snapshot_id"] for c in read.call_args_list], [3, 4]
        )
        wrong = ("sample", 1, None, Decimal("2.50"), src.STAMP0)
        source.execute.side_effect = [[wrong]]
        with self.assertRaisesRegex(
            src.Failure, "CHAIN_SQL_FINAL_TYPED_SHAPE"
        ):
            wire.read_final_sql(source, None, known)
        source.execute.side_effect = [[valid], [(1,)]]
        with self.assertRaisesRegex(
            src.Failure, "CHAIN_SQL_DELETED_KEY_PRESENT"
        ):
            wire.read_final_sql(source, None, known)

    def test_lag_checks_three_independent_source_positions(self):
        """broker最新・独立SQLite・Kafka groupのいずれも推定しない。"""
        source, experiment = Mock(), Mock()
        source.watermark.return_value = (0, 5)
        experiment.offset.return_value = 4
        with (
            patch.object(wire, "local_position", return_value=4) as local,
            patch.object(wire, "committed") as check_group,
        ):
            wire.verify_final_lag(
                source, "consumer", "topic", "sqlite", experiment
            )
            local.assert_called_once_with("sqlite")
            check_group.assert_called_once_with("consumer", "topic", 5)
        with patch.object(wire, "local_position", return_value=3):
            with self.assertRaisesRegex(
                src.Failure, "CHAIN_FINAL_LAG_MISMATCH"
            ):
                wire.verify_final_lag(
                    source, "consumer", "topic", "sqlite", experiment
                )

    def test_readback_failure_prevents_local_checkpoint(self):
        """Raw は残るが後段の失敗を Kafka ACK 前に伝える。"""
        from contract_experiment import ContractBatchError, Experiment

        with closing(sqlite3.connect(":memory:")) as db:
            experiment = Experiment({"version": 1, "tables": [wire.TABLE]}, db)
            with patch.object(
                wire, "ensure", side_effect=[None, src.Failure("NO_WRITE")]
            ):
                with self.assertRaisesRegex(
                    ContractBatchError, "DERIVED_STORAGE_FAILED"
                ):
                    wire.apply(experiment, None, self.fixture_deliveries()[0])
            self.assertEqual(experiment.offset(src.TOPIC, 0), -1)
            self.assertEqual(experiment.raw_count(), 1)
