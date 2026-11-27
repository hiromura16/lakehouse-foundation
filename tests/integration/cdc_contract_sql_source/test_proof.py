"""使い捨てソース試験自体の正負判定を接続なしで検証する。"""

import copy
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from proof import (
    DATABASE,
    INSERTED,
    SNAPSHOT,
    TABLE,
    TOPIC,
    UPDATED,
    Failure,
    Proof,
    checkpoint,
    nanos,
)


def row(op, before, after):
    """固定の合成 source metadata と像を作る。"""
    return {
        "op": op,
        "source": {
            "db": DATABASE,
            "table": TABLE,
            "schema": "dbo",
            "connector": "sqlserver",
            "version": "3.7.1.Final",
            "snapshot": "last" if op == "r" else "false",
            "commit_lsn": "00000001:00000004:0001",
            "change_lsn": "00000001:00000004:0001",
        },
        "before": before,
        "after": after,
    }


def evidence(offset, op, payload, *, key=None, topic=TOPIC, partition=0):
    """Broker record の代わりに、既存 checker の入口へ値を渡す。"""
    if key is None:
        key = json.dumps(
            {"site_id": "sample", "record_id": 1 if op == "r" else 2}
        )
    wire = None if payload is None else json.dumps(payload)
    checkpoint(topic, partition, offset, offset - 1, key, wire, op)


class SourceProofTests(unittest.TestCase):
    """受入条件の壊れやすい境界を固定する。"""

    def test_connect_transient_statuses_retry_read_only(self):
        """起動中の一過性statusは、同じGETだけを再試行する。"""
        proof = Proof(None, None, None, "", "")
        recovering = [409, 500, 502, 503, 504]
        soft = [
            HTTPError("", code, "private response", None, None)
            for code in recovering
        ]
        with patch.object(proof, "http", side_effect=[*soft, []]) as request:
            with patch("proof.time.sleep"):
                self.assertEqual(proof.connect_readiness("/connectors"), [])
        self.assertEqual(
            [call.args for call in request.call_args_list],
            [("/connectors",)] * 6,
        )

    def test_connect_terminal_status_is_not_retried(self):
        """認証/経路の拒否は伏せ字の固定理由を付けて初回で停止する。"""
        proof = Proof(None, None, None, "", "")
        for status in (401, 403, 404):
            error = HTTPError("", status, "private response", None, None)
            with patch.object(proof, "http", side_effect=error) as request:
                with self.assertRaisesRegex(
                    Failure, "CONNECT_READY_HTTP_UNEXPECTED"
                ):
                    proof.connect_readiness("/connector-plugins")
            self.assertEqual(request.call_count, 1)

    def test_connect_retry_deadline_keeps_last_fixed_status(self):
        """期限内の成功がなければ、最終分類で必ず拒否する。"""
        proof = Proof(None, None, None, "", "")
        error = HTTPError("", 503, "private response", None, None)
        with patch.object(proof, "http", side_effect=error):
            with (
                patch("proof.time.sleep"),
                patch("proof.time.monotonic", side_effect=[0, 0, 181]),
            ):
                with self.assertRaisesRegex(Failure, "CONNECT_READY_HTTP_503"):
                    proof.connect_readiness("/connectors")

    def test_snapshot_and_iud_are_not_tombstone(self):
        """snapshot/I/U/D と Kafka null を別々に受理する。"""
        for offset, (op, before, after) in enumerate(
            (
                ("r", None, SNAPSHOT),
                ("c", None, INSERTED),
                ("u", INSERTED, UPDATED),
                ("d", UPDATED, None),
            )
        ):
            evidence(offset, op, row(op, before, after))
        evidence(4, "tombstone", None)
        with self.assertRaisesRegex(Failure, "EXPECTED_KAFKA_NULL"):
            evidence(4, "tombstone", "null")
        with self.assertRaisesRegex(Failure, "BAD_JSON"):
            evidence(3, "d", None)

    def test_exact_key_order_and_offset(self):
        """複合キーの並び/型とKafkaの座標は曖昧に通さない。"""
        wire = row("c", None, INSERTED)
        for key in (
            '{"record_id":2,"site_id":"sample"}',
            '{"site_id":"sample","record_id":true}',
            '{"site_id":"sample","record_id":2,"unexpected":3}',
        ):
            with self.assertRaisesRegex(Failure, "KEY_CONTRACT"):
                evidence(1, "c", wire, key=key)
        with self.assertRaisesRegex(Failure, "BROKER_COORDINATE"):
            evidence(1, "c", wire, partition=1)
        with self.assertRaisesRegex(Failure, "BROKER_COORDINATE"):
            evidence(1, "c", wire, topic="wrong")
        with self.assertRaisesRegex(Failure, "BROKER_COORDINATE"):
            checkpoint(TOPIC, 0, 2, 0, "unused", None, "c")

    def test_new_time_values_and_exact_images(self):
        """値の丸め・数値文字列の正規化・NULL/空を拒否する。"""
        payload = row("u", INSERTED, UPDATED)
        self.assertEqual(nanos("1970-01-01T00:00:00.1234567"), 123456700)
        self.assertEqual(UPDATED["observed_at"] % 1_000_000_000, 345678900)
        for key, value in (
            ("quantity", -1.234),
            ("quantity", "-1.2340"),
            ("label", None),
            ("observed_at", UPDATED["observed_at"] - 900),
        ):
            wrong = copy.deepcopy(payload)
            wrong["after"][key] = value
            with self.assertRaisesRegex(Failure, "TYPED_ROW_IMAGE"):
                evidence(2, "u", wrong)

    def test_unexpected_source_or_incomplete_envelope_refused(self):
        """入口/職務/履歴を失ったメッセージを成功にしない。"""
        payload = row("u", INSERTED, UPDATED)
        for field, value, reason in (
            ("schema", "wrong", "SQL_SOURCE_IDENTITY"),
            ("version", "3.7", "CONNECTOR_SOURCE_VERSION"),
            ("commit_lsn", "00000000:00000000:0000", "SQL_CHANGE_POSITION"),
        ):
            wrong = copy.deepcopy(payload)
            wrong["source"][field] = value
            with self.assertRaisesRegex(Failure, reason):
                evidence(2, "u", wrong)
        wrong = row("r", None, SNAPSHOT)
        del wrong["before"]
        with self.assertRaisesRegex(Failure, "OPERATION_ORDER"):
            evidence(0, "r", wrong)
