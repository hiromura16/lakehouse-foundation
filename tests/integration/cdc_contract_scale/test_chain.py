"""合成broker portと実ローカル保存で、ACK順序を実サービスなしで検査。"""

import importlib.util
import json
import os
import sys
import unittest
from dataclasses import dataclass, replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import chain
import fixture
import source
import storage

CLUSTER = "0123456789abcdefABC_-x"
NATIVE = all(
    importlib.util.find_spec(name) is not None
    for name in ("pyarrow", "pyiceberg", "sqlalchemy")
)


def mock_originals():
    """原本と同じ型の偽物。実Kafka/SQLの証跡には使わない。"""
    result = []
    specs = {member.topic: member for member in fixture.TABLES}
    for record in fixture.deliveries():
        if record["value"] is not None:
            envelope = json.loads(record["value"])
            envelope["source"]["connector"] = "sqlserver"
            envelope["source"]["version"] = source.VERSION
            spec = specs[record["topic"]]
            index = record["offset"]
            if envelope["op"] == "r" and index == 0:
                envelope["source"]["snapshot"] = (
                    "first"
                    if spec == fixture.TABLES[0]
                    else "first_in_data_collection"
                )
            if envelope["op"] == "r" and index == spec.initial - 1:
                envelope["source"]["snapshot"] = (
                    "last"
                    if spec == fixture.TABLES[-1]
                    else "last_in_data_collection"
                )
            envelope["ts_ms"] = 1234
            # Do not canonicalize this representation later.
            record["value"] = json.dumps(
                envelope, ensure_ascii=False, indent=1
            )
        result.append(record)
    return result


def watermarks():
    """偽brokerの各topicに独立した下限と上限を持たせる。"""
    return {topic: (0, end) for topic, end in chain.WANTED.items()}


@dataclass(frozen=True)
class Position:
    """Confluent TopicPartitionと同じ最低限の形だけを使う。"""

    topic: str
    partition: int
    offset: int = chain.OFFSET_INVALID
    error: str | None = None


class MockPort:
    """公開ネットワークへ接続しない、所有group portの代用品。"""

    topic_partition = Position

    def __init__(self, *, fault=None):
        self.client = self
        self.fault = fault
        self.trace = []
        self.commits = []
        self.committed_reads = 0
        self.watermark_reads = 0
        self.positions = dict.fromkeys(chain.WANTED, chain.OFFSET_INVALID)

    def identity(self):
        """代用品のcluster/groupを別の応答として観測する。"""
        group = "wrong.fixture" if self.fault == "identity" else chain.GROUP
        return CLUSTER, group

    def get_watermark_offsets(self, coord, *, timeout, cached):
        """全表の実体に相当する読み取り回答。キャッシュは禁止。"""
        if cached or timeout != 5:
            raise AssertionError("unexpected watermark mode")
        self.watermark_reads += 1
        end = chain.WANTED[coord.topic]
        changed = self.fault == "source_drift" and self.watermark_reads == 6
        after = self.fault == "post_commit_drift" and bool(self.commits)
        return (0, end + int(changed or after))

    def committed(self, coords, *, timeout):
        """同期ACK応答とは別のgroup観測を返す。"""
        if timeout != 8:
            raise AssertionError("unexpected commit read mode")
        self.committed_reads += 1
        values = self.positions.copy()
        if self.fault == "group_drift" and self.committed_reads == 2:
            values[coords[0].topic] = 0
        if self.fault == "readback" and self.commits:
            values[coords[-1].topic] -= 1
        return [replace(coord, offset=values[coord.topic]) for coord in coords]

    def commit(self, *, offsets, asynchronous):
        """一度だけ代用ACKを記録。ストレージとの呼出順も記録する。"""
        if asynchronous is not False:
            raise AssertionError("not synchronous")
        self.commits.append(offsets)
        self.trace.append("commit")
        self.positions.update({entry.topic: entry.offset for entry in offsets})
        if self.fault == "response":
            return [*offsets[:-1], replace(offsets[-1], error="bad synthetic")]
        return list(reversed(offsets))


class DummySink:
    """速い境界の拒否テスト専用。実保存の肯定テストには使わない。"""

    def __init__(self, port, fault=None):
        self.port = port
        self.fault = fault
        self.records = []
        self.is_reopened = False

    def __enter__(self):
        return self

    def __exit__(self, *_detail):
        pass

    def write(self, records):
        """失敗分岐では途中成果をACK可能とみなさない。"""
        if self.fault == "write":
            raise ValueError("DERIVED_STORAGE_FAILED")
        self.records.extend(records)
        return {"accepted": len(records)}

    def offsets(self):
        """別接続から再読した位置に相当する結果を返す。"""
        result = {}
        for record in self.records:
            result[record["topic"]] = record["offset"]
        if self.fault == "offset" and self.is_reopened:
            skipped = sorted(result)[0]
            result[skipped] -= 1
        return result

    def verify_final(self, original):
        """writer戻りではなく完全な元配送リストを確認する。"""
        if self.records != original:
            raise AssertionError("wire changed")
        self.port.trace.append(
            "verified_again" if self.is_reopened else "verified"
        )
        return {
            "raw_rows": len(self.records),
            "current_rows": 112,
            "silver_rows": 112,
        }

    def reopen(self):
        """再接続の順序を受領書に残す。"""
        self.is_reopened = True
        self.port.trace.append("reopen")


class ChainTests(unittest.TestCase):
    """主な拒否条件が、ACK前かACK不明かを区別する。"""

    def setUp(self):
        self.records = mock_originals()
        self.handoff = chain.capture(
            self.records, watermarks(), cluster=CLUSTER, group=chain.GROUP
        )

    def test_malformed_or_reconstructed_native_does_not_form_handoff(self):
        """件数だけや同じ内容のモデル配送では合格しない。"""
        model = list(fixture.deliveries())
        with self.assertRaisesRegex(ValueError, "CHAIN_NATIVE_ORIGIN"):
            chain.capture(
                model, watermarks(), cluster=CLUSTER, group=chain.GROUP
            )
        with self.assertRaisesRegex(ValueError, "CHAIN_RECORD_COUNT"):
            chain.capture(
                self.records[:-1],
                watermarks(),
                cluster=CLUSTER,
                group=chain.GROUP,
            )
        incorrect = watermarks()
        key = next(iter(incorrect))
        incorrect[key] = (False, incorrect[key][1])
        with self.assertRaisesRegex(ValueError, "CHAIN_SOURCE_WATERMARK"):
            chain.capture(
                self.records, incorrect, cluster=CLUSTER, group=chain.GROUP
            )

    def test_before_commit_drift_or_local_storage_stops_without_ack(self):
        """5対のうち1つでも別group/高水位/完了位置ならACKしない。"""
        for port_fault, store_fault, code in (
            ("identity", None, "CHAIN_PORT_IDENTITY"),
            ("source_drift", None, "CHAIN_SOURCE_WATERMARK"),
            ("group_drift", None, "CHAIN_GROUP_NOT_FRESH"),
            (None, "offset", "CHAIN_LOCAL_NOT_COMPLETE"),
            (None, "write", "DERIVED_STORAGE_FAILED"),
        ):
            with self.subTest(port=port_fault, storage=store_fault):
                port = MockPort(fault=port_fault)
                with self.assertRaisesRegex(ValueError, code):
                    chain.run(
                        self.handoff,
                        port,
                        store_factory=lambda: DummySink(port, store_fault),
                    )
                self.assertEqual(port.commits, [])

    def test_after_commit_failure_is_uncertain_without_retry(self):
        """同期応答や再照会の異常はACKなしとみなさず、再送しない。"""
        for fault in ("response", "readback", "post_commit_drift"):
            with self.subTest(fault=fault):
                port = MockPort(fault=fault)
                with self.assertRaisesRegex(
                    ValueError, "^CHAIN_ACK_UNCERTAIN$"
                ):
                    chain.run(
                        self.handoff,
                        port,
                        store_factory=lambda: DummySink(port),
                    )
                self.assertEqual(len(port.commits), 1)

    def test_store_reopen_and_independent_verification_precede_only_commit(
        self,
    ):
        """順序の証明。実保存の肯定検査は下の実SDKテストで行う。"""
        port = MockPort()
        result = chain.run(
            self.handoff, port, store_factory=lambda: DummySink(port)
        )
        self.assertEqual(
            port.trace, ["verified", "reopen", "verified_again", "commit"]
        )
        self.assertEqual(len(port.commits), 1)
        self.assertEqual(port.committed_reads, 3)
        self.assertEqual(result["outstanding"], 0)

    @unittest.skipUnless(NATIVE, "requires pinned local Arrow/SqlCatalog")
    def test_readback_uses_real_storage_and_original_wire(self):
        """元配送相当のrecordを実保存し、代用brokerだけへACKする。"""
        # Mutating the caller's list cannot alter the sealed handoff.
        self.records[0]["value"] = None
        self.assertIsNotNone(self.handoff.records[0].value)
        port = MockPort()

        class AuditedStorage(storage.Harness):
            """actual storage results remain the authoritative local check."""

            def reopen(self):
                super().reopen()
                port.trace.append("reopened_real_storage")

        summary = chain.run(self.handoff, port, store_factory=AuditedStorage)
        self.assertEqual(port.trace, ["reopened_real_storage", "commit"])
        self.assertEqual(len(port.commits), 1)
        self.assertEqual(
            {row.topic: row.offset for row in port.commits[0]}, chain.WANTED
        )
        self.assertEqual(
            summary,
            {
                "source_records": 1392,
                "local_partitions": 5,
                "source_partitions": 5,
                "outstanding": 0,
                "raw_rows": 1392,
                "current_rows": 112,
                "silver_rows": 112,
            },
        )


if __name__ == "__main__":
    unittest.main()


class NativeCallbackTest(unittest.TestCase):
    """同じowned consumerをsourceとsinkで維持する接合部を検査。"""

    def test_exact_native_port_while_consumer_still_open(self):
        """実sourceの検証前や受信consumerのclose後にはACKを渡さない。"""
        events = mock_originals()
        snapshots = []
        streaming = []
        for row in events:
            if (
                row["value"] is not None
                and json.loads(row["value"])["op"] == "r"
            ):
                snapshots.append(row)
            else:
                streaming.append(row)
        client = Mock()
        factory = Mock(return_value=client)
        kafka = SimpleNamespace(Consumer=factory, TopicPartition=Position)
        admin = Mock()
        admin.list_topics.return_value = SimpleNamespace(cluster_id=CLUSTER)
        driver = source.Scale(
            None, kafka, Mock(return_value=admin), object, CLUSTER, "", ""
        )
        client.get_watermark_offsets.side_effect = lambda coord, **_kwargs: (
            watermarks()[coord.topic]
        )
        passed = {"source_records": 1392}
        reconcile = Mock(wraps=fixture.reconcile)

        def after(records, positions, port):
            client.close.assert_not_called()
            reconcile.assert_called_once()
            self.assertIs(type(port), source.NativePort)
            self.assertIs(port.client, client)
            self.assertEqual(port.identity(), (CLUSTER, source.CHAIN_GROUP))
            self.assertEqual(positions, watermarks())
            self.assertTrue(
                all(
                    any(row is observed for observed in events)
                    for row in records
                )
            )
            return passed

        with (
            patch.object(driver, "topic_inventory"),
            patch.object(driver, "connect_identity"),
            patch.object(driver, "connect_readiness", return_value=[]),
            patch.object(driver, "await_sql"),
            patch.object(driver, "provision"),
            patch.object(driver, "write_sql"),
            patch.object(driver, "seed_captured"),
            patch.object(driver, "register"),
            patch.object(
                driver, "receive", side_effect=[snapshots, streaming]
            ),
            patch.object(driver, "live", return_value=True),
            patch.object(driver, "final_sql"),
            patch.object(fixture, "reconcile", reconcile),
        ):
            self.assertIs(driver.run(after_reconciled=after), passed)
        config = factory.call_args.args[0]
        self.assertEqual(config["group.id"], source.CHAIN_GROUP)
        self.assertIs(config["enable.auto.commit"], False)
        self.assertIs(config["enable.auto.offset.store"], False)
        self.assertEqual(client.get_watermark_offsets.call_count, 5)
        self.assertTrue(
            all(
                call.kwargs == {"timeout": 5, "cached": False}
                for call in client.get_watermark_offsets.call_args_list
            )
        )
        client.close.assert_called_once_with()

    def test_only_created_native_port_is_accepted_by_command_entry(self):
        """他のclientや外部から模したportを実経路へ昇格しない。"""
        with self.assertRaisesRegex(ValueError, "CHAIN_NATIVE_PORT"):
            chain.native([], {}, MockPort())

    def test_chain_mode_is_denied_before_requests_without_opt_in(self):
        """Python入口でも別経路のrun-chainを誤って有効化しない。"""
        with (
            patch.object(source, "require_hosted", return_value=CLUSTER),
            patch.object(sys, "argv", ["source.py", "run-chain"]),
            patch.dict(os.environ, {}, clear=True),
            patch.object(source.base, "fixture_passwords") as secrets,
            patch.object(sys, "stderr", new_callable=StringIO) as captured,
        ):
            self.assertEqual(source.main(), 1)
            self.assertEqual(
                captured.getvalue(),
                "FAIL: SCALE_ENTRY/SCALE_CHAIN_MODE_REQUIRED\n",
            )
            secrets.assert_not_called()

    def test_native_emits_only_allowlisted_ack_uncertainty(self):
        """通信例外を載せず、不確定なACKを固定コードで示す。"""
        owner = Mock()
        owner.metadata.return_value = SimpleNamespace(cluster_id=CLUSTER)
        port = source.NativePort(Mock(), Position, owner, source.CHAIN_GROUP)
        with (
            patch.object(chain, "capture", return_value=object()),
            patch.object(
                chain, "run", side_effect=ValueError("CHAIN_ACK_UNCERTAIN")
            ) as apply,
        ):
            with self.assertRaisesRegex(
                source.base.Failure, "^SCALE_CHAIN_ACK_UNCERTAIN$"
            ):
                chain.native([], {}, port)
            self.assertEqual(apply.call_count, 1)
