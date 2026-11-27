"""再開時の保存位置、実配送の受け渡し、fail-closed 境界を単体確認。"""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import proof as src
from restart import (
    Restart,
    fingerprint,
    observed_offset,
    saved_stream_position,
    validate_receipt,
)
from test_proof import row


def offsets(**fields):
    """専用 SQL source の Kafka Connect offset REST 形。"""
    return {
        "offsets": [
            {
                "partition": {"server": src.PREFIX, "database": src.DATABASE},
                "offset": {
                    "commit_lsn": "00000001:00000004:0001",
                    "change_lsn": "00000001:00000004:0002",
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
        value = offsets()
        self.assertIsNotNone(saved_stream_position(value))
        bad_partition = offsets()
        bad_partition["offsets"][0]["partition"]["database"] = "not-fixture"
        for bad in (
            {"offsets": []},
            {"offsets": [value["offsets"][0]] * 2},
            bad_partition,
            offsets(snapshot_completed=False),
            offsets(commit_lsn="00000000:00000000:0000"),
            offsets(change_lsn="00000000:00000000:0000"),
            offsets(event_serial_no=True),
            offsets(command_id="1"),
        ):
            with self.subTest(bad_type=type(bad)):
                self.assertIsNone(saved_stream_position(bad))
        self.assertNotEqual(
            saved_stream_position(value),
            saved_stream_position(offsets(event_serial_no=2)),
        )

    def test_snapshot_only_offsets_do_not_attest_a_streaming_insert(self):
        """snapshot完了らしい印でも実streamingのLSNと混同しない。"""
        for snapshot in ("initial", "INITIAL", "incremental", True):
            actual = offsets(snapshot=snapshot, snapshot_completed=True)
            self.assertEqual(observed_offset(actual), (None, "SNAPSHOT_PHASE"))
        self.assertEqual(
            observed_offset(offsets(change_lsn="NULL", command_id=-1)),
            (None, "CHANGE_LSN"),
        )
        self.assertEqual(observed_offset({"offsets": []}), (None, "EMPTY"))

    def test_initial_insert_fence_after_verified_snapshot(self):
        """必ず初回rを読んでからIを行い、r/cのbytesとcのLSNを返す。"""
        worker = Restart(
            None, SimpleNamespace(TopicPartition=Mock()), None, "", ""
        )
        consumer = Mock()
        recorded = []

        def consume(_consumer, op):
            recorded.append(op)
            return self.records()[0 if op == "r" else 1]

        with (
            patch.object(worker, "consumer", return_value=consumer),
            patch.object(worker, "watermark", side_effect=[(0, 1), (0, 2)]),
            patch.object(worker, "evidence", side_effect=consume),
            patch.object(
                worker,
                "insert",
                side_effect=lambda *_: recorded.append("INSERT"),
            ),
        ):
            digest, lsn = worker.before_committed_insert()
        self.assertEqual(recorded, ["r", "INSERT", "c"])
        self.assertEqual(digest, worker.prefix_digest(self.records()[:2]))
        self.assertEqual(lsn, "00000001:00000004:0001")
        consumer.close.assert_called_once_with()

    def test_post_restart_does_not_insert_second_time(self):
        """共通sourceは従来のI/U/D、再起動モードだけU/Dから始める。"""
        worker = Restart(None, None, None, "", "")
        for resume, expected in (
            (False, ["c", "u", "d", "tombstone"]),
            (True, ["u", "d", "tombstone"]),
        ):
            with (
                patch.object(worker, "insert") as insert,
                patch.object(worker, "execute"),
                patch.object(worker, "evidence") as called,
            ):
                worker.changes(object(), after_insert=resume)
            self.assertEqual(insert.call_count, 0 if resume else 1)
            self.assertEqual(
                [c.args[1] for c in called.call_args_list], expected
            )

    def test_before_stop_receipt_requires_matching_insert_position(self):
        """INSERTの確定位置がなければ受入証跡を発行しない。"""
        worker = Restart(None, None, None, "", "")
        lsn = "00000001:00000004:0001"
        with (
            patch.object(worker, "await_sql"),
            patch.object(worker, "provision"),
            patch.object(worker, "captured_seed"),
            patch.object(worker, "register"),
            patch.object(
                worker, "before_committed_insert", return_value=("prefix", lsn)
            ),
            patch.object(
                worker, "durable", return_value="position"
            ) as durable,
            patch.object(worker, "history", return_value="schema") as history,
        ):
            self.assertEqual(
                worker.before(),
                {
                    "prefix": "prefix",
                    "position": "position",
                    "history": "schema",
                },
            )
            durable.assert_called_once_with(expected_lsn=lsn)
            durable.side_effect = src.Failure("EXPECTED_LSN_NOT_COMMITTED")
            with self.assertRaisesRegex(
                src.Failure, "EXPECTED_LSN_NOT_COMMITTED"
            ):
                worker.before()
            self.assertEqual(history.call_count, 1)

    def test_after_restart_changed_first_two_records_blocks_update(self):
        """再読した元prefixが違えばUPDATEやDELETEへ進まない。"""
        worker = Restart(None, None, None, "", "")
        with (
            patch.object(worker, "connect_identity"),
            patch.object(worker, "durable", return_value="position"),
            patch.object(worker, "history", return_value="schema"),
            patch.object(worker, "retained_prefix", return_value="different"),
            patch.object(worker, "changes") as changes,
        ):
            with self.assertRaisesRegex(src.Failure, "PREFIX_RECORDS_CHANGED"):
                worker.after(
                    {
                        "prefix": "prefix",
                        "position": "position",
                        "history": "schema",
                    },
                    "synthetic-lsn-digest",
                )
            changes.assert_not_called()

    def test_stopped_update_reads_real_cdc_coordinate_and_kafka_stays_two(
        self,
    ):
        """現在値とCDC捕捉位置は実queryから、Kafka範囲は前後で検証。"""
        worker = Restart(None, None, None, "", "")
        lsn = "00000001:00000004:0001"
        raw = bytes.fromhex(lsn.replace(":", ""))
        consumer = Mock()
        with (
            patch.object(worker, "consumer", return_value=consumer),
            patch.object(worker, "watermark", side_effect=[(0, 2), (0, 2)]),
            patch.object(
                worker,
                "execute",
                side_effect=[[(1,)], (), [(1,)], [(raw,)]],
            ) as statements,
        ):
            self.assertEqual(worker.stopped_update(), fingerprint(lsn))
        queries = [args.args[1] for args in statements.call_args_list]
        self.assertIn("label IS NULL", queries[0])
        self.assertIn("UPDATE dbo.fixture_orders", queries[1])
        self.assertIn("DATALENGTH(label)=0", queries[2])
        self.assertIn("__$operation]=4", queries[3])
        consumer.close.assert_called_once_with()

    def test_stopped_update_rejects_early_kafka_delivery(self):
        """停止中に配送が進んでもcatch-up成功条件にしない。"""
        worker = Restart(None, None, None, "", "")
        with (
            patch.object(worker, "consumer", return_value=Mock()),
            patch.object(worker, "watermark", side_effect=[(0, 2), (0, 3)]),
            patch.object(worker, "verify_order"),
            patch.object(worker, "update_target"),
            patch.object(worker, "update_capture", return_value="proof"),
        ):
            with self.assertRaisesRegex(src.Failure, "STOPPED_KAFKA_ADVANCED"):
                worker.stopped_update()

    def test_catchup_resume_allows_insert_or_exact_update_offset_only(self):
        """配信後・REST確定前後の競合は許すが、別の保存位置を拒否。"""
        for startup in ("insert", "update", "unknown"):
            worker = Restart(
                None, SimpleNamespace(TopicPartition=Mock()), None, "", ""
            )
            update, deletion = self.records()[2:4]
            after_lsn = "00000001:00000004:0001"
            require_digest = fingerprint(after_lsn)
            with (
                patch.object(worker, "connect_identity"),
                patch.object(worker, "consumer", return_value=Mock()),
                patch.object(
                    worker,
                    "durable",
                    side_effect=[startup, "update", "delete"],
                ) as saved,
                patch.object(worker, "history", return_value="schema"),
                patch.object(worker, "retained_prefix", return_value="prefix"),
                patch.object(worker, "evidence", return_value=update),
                patch.object(
                    worker, "watermark", side_effect=[(0, 3), (0, 5)]
                ),
                patch.object(worker, "verify_final"),
                patch.object(
                    worker, "delete_target", return_value=deletion
                ) as delete,
                patch("builtins.print"),
            ):
                prior = {
                    "prefix": "prefix",
                    "history": "schema",
                    "position": "insert",
                }
                if startup == "unknown":
                    with self.assertRaisesRegex(
                        src.Failure, "UNEXPECTED_RESTART_POSITION"
                    ):
                        worker.after(prior, require_digest)
                    delete.assert_not_called()
                else:
                    worker.after(prior, require_digest)
                    self.assertEqual(
                        saved.call_args_list[0].kwargs, {"startup": True}
                    )
                    self.assertEqual(
                        saved.call_args_list[1].kwargs,
                        {"expected_lsn": after_lsn},
                    )
                    delete.assert_called_once()

    def test_unrelated_update_does_not_advance_to_delete(self):
        """正しい行の形でも停止中SQLで観測したLSNと別なら拒否。"""
        worker = Restart(
            None, SimpleNamespace(TopicPartition=Mock()), None, "", ""
        )
        with (
            patch.object(worker, "connect_identity"),
            patch.object(worker, "durable", return_value="insert") as saved,
            patch.object(worker, "history", return_value="schema"),
            patch.object(worker, "retained_prefix", return_value="prefix"),
            patch.object(worker, "consumer", return_value=Mock()),
            patch.object(worker, "evidence", return_value=self.records()[2]),
            patch.object(worker, "delete_target") as delete,
        ):
            with self.assertRaisesRegex(
                src.Failure, "STOPPED_SQL_KAFKA_LSN_MISMATCH"
            ):
                worker.after(
                    {
                        "prefix": "prefix",
                        "history": "schema",
                        "position": "insert",
                    },
                    fingerprint("different"),
                )
            saved.assert_called_once_with(startup=True)
            delete.assert_not_called()

    def test_seed_capture_requires_real_positive_single_row(self):
        """SQLの空結果や複数の捕捉を成功にしない。"""
        worker = Restart(None, None, None, "", "")
        with patch.object(
            worker, "execute", side_effect=[[], [(1,)]]
        ) as query:
            with patch("restart.time.sleep"):
                worker.captured_seed()
            self.assertEqual(query.call_count, 2)
            self.assertTrue(
                all(
                    call.kwargs.get("reader") is True
                    for call in query.call_args_list
                )
            )
        with patch.object(worker, "execute", return_value=[(1,), (1,)]):
            with self.assertRaisesRegex(
                src.Failure, "NON_UNIQUE_CAPTURED_SEED"
            ):
                worker.captured_seed()
        with patch.object(worker, "execute", return_value=[]):
            with (
                patch("restart.time.sleep"),
                patch("restart.time.monotonic", side_effect=[0, 0, 56]),
            ):
                with self.assertRaisesRegex(
                    src.Failure, "ACTUAL_SEED_CAPTURE_NOT_OBSERVED"
                ):
                    worker.captured_seed()

    def test_final_position_can_require_delete_lsn(self):
        """先行 INSERT や UPDATE による進捗で DELETE を証明しない。"""
        lsn = "00000001:00000004:000a"
        self.assertIsNone(saved_stream_position(offsets(), expected_lsn=lsn))
        self.assertIsNotNone(
            saved_stream_position(
                offsets(
                    commit_lsn=lsn.upper(),
                    change_lsn="00000001:00000004:000b",
                    command_id=1,
                ),
                expected_lsn=lsn,
            )
        )

    def test_receipt_requires_each_hash(self):
        """存在だけのファイルや生データは通さない。"""
        value = {
            k: fingerprint([k]) for k in ("position", "prefix", "history")
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
