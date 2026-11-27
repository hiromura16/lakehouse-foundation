"""固定の fault 判定と同期 ACK 条件をサービス接続なしで検査する。"""

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from kafka_transport import KafkaSession
from proof import (
    DOC,
    TOPIC,
    ProofFailure,
    acknowledge,
    fixture_record,
    reopened,
    require_resume_alignment,
)


def receipt(**kwargs):
    """broker 応答が返した型と座標だけを模擬する。"""
    row = dict(topic=TOPIC, partition=0, offset=1, error=None)
    row.update(kwargs)
    return SimpleNamespace(**row)


class ReceiptTests(unittest.TestCase):
    """同じ期待位置の実 ACK でだけ配送を完了扱いにする。"""

    def test_positive(self):
        """固定座標の同期応答を返す。"""
        self.assertEqual(KafkaSession.check([receipt()], 1), 1)

    def test_bad_sync_receipt_does_not_pass(self):
        """空、重複、誤座標、応答エラーを成功扱いしない。"""
        for result in (
            None,
            [],
            [receipt(), receipt()],
            [receipt(topic="synthetic.wrong")],
            [receipt(partition=2)],
            [receipt(offset=-1)],
            [receipt(offset=0)],
            [receipt(error=object())],
        ):
            with self.subTest(result=result):
                with self.assertRaisesRegex(
                    ProofFailure, "BROKER_POSITION_NOT_ATTESTED"
                ):
                    KafkaSession.check(result, 1)

    def test_missing_state_cannot_be_recreated_on_restart(self):
        """復旧時は初期化せず、ACKもできない。"""
        with TemporaryDirectory() as path:
            directory = Path(path)
            with self.assertRaisesRegex(ProofFailure, "LOCAL_STATE_MISSING"):
                with reopened(directory, existing=True):
                    self.fail("unexpected reopen")
            self.assertEqual(list(directory.iterdir()), [])

    def test_inconsistent_local_checkpoint_never_calls_ack(self):
        """別の読み取り接続が旧完了位置なら broker ACK を呼ばない。"""
        with TemporaryDirectory() as path:
            directory = Path(path)
            connection = sqlite3.connect(directory / "state.db")
            try:
                with connection:
                    connection.execute(
                        "CREATE TABLE checkpoints(topic,part,off)"
                    )
                    connection.execute(
                        "INSERT INTO checkpoints VALUES(?,?,?)", (TOPIC, 0, 0)
                    )
                with self.assertRaisesRegex(
                    ProofFailure, "LOCAL_CHECKPOINT_NOT_OBSERVED"
                ):
                    acknowledge(directory, None, 2)
            finally:
                connection.close()

    def test_empty_and_partial_schema_are_not_adopted(self):
        """ファイルがあっても空・必須表欠落なら初期化をしない。"""
        from contract_experiment import ContractBatchError, Experiment

        with TemporaryDirectory() as path:
            root = Path(path)
            (root / "catalog.db").touch()
            with closing(sqlite3.connect(root / "state.db")) as connection:
                with self.assertRaisesRegex(
                    ProofFailure, "LOCAL_STATE_SCHEMA_MISSING"
                ):
                    with reopened(root, existing=True):
                        self.fail("unexpected reopen")
                Experiment(DOC, connection)
                with connection:
                    connection.execute("DROP TABLE accepted")
                with self.assertRaisesRegex(
                    ContractBatchError, "EXPERIMENT_IDENTITY_MISMATCH"
                ):
                    with reopened(root, existing=True):
                        self.fail("unexpected reopen")

    def test_missing_acceptance_rows_detected_by_explicit_replay(self):
        """schema正常の行欠落は起動ではなく明示 replay で発覚する。"""
        from contract_experiment import ContractBatchError, Experiment

        with TemporaryDirectory() as path:
            state = Path(path) / "state.db"
            with closing(sqlite3.connect(state)) as connection:
                experiment = Experiment(DOC, connection)
                experiment.process([fixture_record(0)])
                with connection:
                    connection.execute("DELETE FROM accepted")
            with closing(sqlite3.connect(state)) as connection:
                experiment = Experiment(DOC, connection)
                self.assertEqual(experiment.offset(TOPIC, 0), 0)
                with self.assertRaisesRegex(
                    ContractBatchError, "REPLAY_STATE_MISMATCH"
                ):
                    experiment.replay()

    def test_local_commit_ahead_of_broker_is_not_repaired(self):
        """local commit後・ACK前の中断では再開を拒否し、自動ACKしない。"""
        from contract_experiment import Experiment

        with TemporaryDirectory() as path:
            state = Path(path) / "state.db"
            with closing(sqlite3.connect(state)) as connection:
                Experiment(DOC, connection).process([fixture_record(0)])
            with closing(sqlite3.connect(state)) as connection:
                restored = Experiment(DOC, connection)
                with self.assertRaisesRegex(
                    ProofFailure, "BROKER_RESUME_POSITION"
                ):
                    require_resume_alignment(
                        restored, SimpleNamespace(committed=lambda: 0)
                    )
                self.assertEqual(restored.offset(TOPIC, 0), 0)


if __name__ == "__main__":
    unittest.main()
