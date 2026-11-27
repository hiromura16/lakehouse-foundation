"""再開時の保存位置、実配送の受け渡し、fail-closed 境界を単体確認。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import proof as src
from restart import Restart, fingerprint, saved_snapshot, validate_receipt
from test_proof import row


def offsets(**fields):
    """専用 SQL source の Kafka Connect offset REST 形。"""
    return {
        "offsets": [
            {
                "partition": {"server": src.PREFIX, "database": src.DATABASE},
                "offset": {
                    "commit_lsn": "00000001:00000004:0001",
                    "change_lsn": None,
                    "event_serial_no": 1,
                    "command_id": None,
                    **fields,
                },
            }
        ]
    }


def delivered(index, op, before=None, after=None):
    """テスト用 wire bytes。同じオブジェクトを返すことまで確認する。"""
    return SimpleNamespace(
        error=lambda: None,
        topic=lambda: src.TOPIC,
        partition=lambda: 0,
        offset=lambda: index,
        key=lambda: json.dumps(
            {"site_id": "sample", "record_id": 1 if op == "r" else 2}
        ).encode(),
        value=lambda: (
            None
            if op == "tombstone"
            else json.dumps(row(op, before, after)).encode()
        ),
    )


class RestartTests(unittest.TestCase):
    """ネットワークを使わずに再開試験自体の入力条件を固定。"""

    def test_exact_persisted_position(self):
        """空・未完了・異なるsourceを保存された位置と誤認しない。"""
        value = offsets(snapshot="initial", snapshot_completed=True)
        same_position = offsets()
        self.assertEqual(saved_snapshot(value), saved_snapshot(same_position))
        self.assertIsNotNone(saved_snapshot(value))
        bad_partition = offsets()
        bad_partition["offsets"][0]["partition"]["database"] = "not-fixture"
        for bad in (
            {"offsets": []},
            {"offsets": [value["offsets"][0]] * 2},
            bad_partition,
            offsets(snapshot="initial", snapshot_completed=False),
            offsets(snapshot="incremental", snapshot_completed=True),
            offsets(snapshot_completed=False),
            offsets(commit_lsn="00000000:00000000:0000"),
            offsets(event_serial_no=True),
            offsets(command_id="1"),
        ):
            with self.subTest(bad_type=type(bad)):
                self.assertIsNone(saved_snapshot(bad))
        self.assertNotEqual(
            saved_snapshot(value), saved_snapshot(offsets(event_serial_no=2))
        )

    def test_final_position_can_require_delete_lsn(self):
        """先行 INSERT や UPDATE による進捗で DELETE を証明しない。"""
        lsn = "00000001:00000004:000a"
        self.assertIsNone(saved_snapshot(offsets(), expected_lsn=lsn))
        self.assertIsNotNone(
            saved_snapshot(offsets(commit_lsn=lsn.upper()), expected_lsn=lsn)
        )

    def test_receipt_requires_each_hash(self):
        """存在だけのファイルや生データは通さない。"""
        value = {
            k: fingerprint([k]) for k in ("position", "snapshot", "history")
        }
        self.assertEqual(validate_receipt(value), value)
        for bad in ({}, dict(value, position="missing"), dict(value, extra=1)):
            with self.assertRaisesRegex(
                src.Failure, "INVALID_RESTART_RECEIPT"
            ):
                validate_receipt(bad)

    def make_handoff(self, records, bounds=((0, 5), (0, 5))):
        """次の broker 絶対座標とネイティブ message を別々に選ぶ。"""
        consumer = Mock()
        consumer.poll.side_effect = records
        proof = Restart(
            None, SimpleNamespace(TopicPartition=Mock()), None, "", ""
        )
        with patch.object(proof, "consumer", return_value=consumer):
            with patch.object(proof, "watermark", side_effect=bounds):
                return list(proof.verified_records())

    def records(self):
        """型・履歴・キーを失わない五つの元メッセージ。"""
        return [
            delivered(0, "r", after=src.SNAPSHOT),
            delivered(1, "c", after=src.INSERTED),
            delivered(2, "u", before=src.INSERTED, after=src.UPDATED),
            delivered(3, "d", before=src.UPDATED),
            delivered(4, "tombstone"),
        ]

    def test_handoff_reuses_objects_and_tombstone(self):
        """下流用にイベントを合成し直さず、Kafka nullもそのまま渡す。"""
        records = self.records()
        observed = self.make_handoff(records)
        self.assertEqual(len(observed), 5)
        self.assertTrue(
            all(x is y for x, y in zip(observed, records, strict=True))
        )
        self.assertIsNone(observed[-1].value())

    def test_handoff_denies_another_snapshot_or_late_delivery(self):
        """重複 snapshot や余分なレコードを正常受入にしない。"""
        records = self.records()
        records[1] = records[0]
        with self.assertRaisesRegex(
            src.Failure, "UNEXPECTED_RESTART_DELIVERY"
        ):
            self.make_handoff(records)
        with self.assertRaisesRegex(src.Failure, "HANDOFF_CHANGED"):
            self.make_handoff(self.records(), ((0, 5), (0, 6)))
