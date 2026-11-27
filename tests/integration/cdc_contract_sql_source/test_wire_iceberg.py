"""契約consumer自体とSQL由来wire形式をローカルSQLiteだけで突き合わせる。"""

import json
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

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
