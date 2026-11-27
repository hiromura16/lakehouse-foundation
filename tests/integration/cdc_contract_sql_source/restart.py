"""同じ合成 SQL source worker を停止・再始動した前後の永続境界。"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

import proof as src

RECEIPT = Path("/restart-state/restart.json")
LSN = re.compile(r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}:[0-9a-fA-F]{4}")
SHA = re.compile(r"[0-9a-f]{64}")
MAX_HISTORY = 64


def fingerprint(value):
    """状態の値そのものを runner レシートや公開ログへ移さない。"""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def saved_snapshot(body, *, expected_lsn=None):
    """Kafka Connect REST が報告する確定済みの一 source partition。"""
    if not isinstance(body, dict):
        return None
    entries = body.get("offsets")
    if not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry, dict):
        return None
    partition, position = entry.get("partition"), entry.get("offset")
    if partition != {"server": src.PREFIX, "database": src.DATABASE}:
        return None
    if not isinstance(position, dict):
        return None
    if position.get("snapshot_completed") is False or (
        "snapshot" in position
        and (
            position["snapshot"] != "initial"
            or position.get("snapshot_completed") is not True
        )
    ):
        return None
    commit_lsn = position.get("commit_lsn")
    if (
        type(commit_lsn) is not str
        or not LSN.fullmatch(commit_lsn)
        or set(commit_lsn) <= {"0", ":"}
        or expected_lsn is not None
        and commit_lsn.lower() != expected_lsn.lower()
    ):
        return None
    change_lsn = position.get("change_lsn")
    serial = position.get("event_serial_no")
    command = position.get("command_id")
    if (
        change_lsn is not None
        and (type(change_lsn) is not str or not LSN.fullmatch(change_lsn))
        or type(serial) is not int
        or serial < 0
        or command is not None
        and (type(command) is not int or command < 0)
    ):
        return None
    return fingerprint(
        [partition, commit_lsn.lower(), change_lsn, serial, command]
    )


def validate_receipt(document):
    """存在だけでは成功扱いせず、先行証跡は固定形のみ受理する。"""
    src.require(
        isinstance(document, dict)
        and set(document) == {"position", "history", "snapshot"}
        and all(
            type(x) is str and SHA.fullmatch(x) for x in document.values()
        ),
        "INVALID_RESTART_RECEIPT",
    )
    return document


class Restart(src.Proof):
    """旧 source 検証と同じSQL・topic・event validator を使う。"""

    def consumer(self):
        """offset を変更せず、絶対 Kafka 座標から比較する専用 session。"""
        return self.kafka.Consumer(
            {
                "bootstrap.servers": src.BROKER,
                "group.id": "fixture-source-restart-observer",
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "allow.auto.create.topics": False,
                "log_level": 0,
            }
        )

    def message(self, consumer, topic, index):
        """固定 topic・partition・offset 以外の配送を受理しない。"""
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if msg is None:
                self.live_task()
                continue
            src.require(
                msg.error() is None
                and (msg.topic(), msg.partition(), msg.offset())
                == (topic, 0, index),
                "UNEXPECTED_RESTART_DELIVERY",
            )
            return msg
        raise src.Failure("RESTART_READ_TIMEOUT")

    def watermark(self, consumer, topic):
        """broker の現在値を取得する。固定期待値で補完しない。"""
        return consumer.get_watermark_offsets(
            self.kafka.TopicPartition(topic, 0), timeout=6
        )

    def snapshot(self):
        """再開後も同じ broker record を wire bytes で照合する。"""
        consumer = self.consumer()
        try:
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 0)])
            msg = self.message(consumer, src.TOPIC, 0)
            src.checkpoint(src.TOPIC, 0, 0, -1, msg.key(), msg.value(), "r")
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 1),
                "EXPECTED_ONE_SNAPSHOT",
            )
            return fingerprint([msg.key().hex(), msg.value().hex()])
        finally:
            consumer.close()

    def history(self):
        """空・欠落・重複・変化を許さず実履歴を順序つきでハッシュ化。"""
        consumer = self.consumer()
        try:
            low, high = self.watermark(consumer, src.HISTORY)
            src.require(
                low == 0 and 0 < high <= MAX_HISTORY, "HISTORY_NOT_RETAINED"
            )
            consumer.assign([self.kafka.TopicPartition(src.HISTORY, 0, 0)])
            rows = []
            for i in range(high):
                msg = self.message(consumer, src.HISTORY, i)
                key, value = msg.key(), msg.value()
                src.require(
                    type(value) is bytes and 0 < len(value) <= 262144,
                    "HISTORY_RECORD_SHAPE",
                )
                rows.append(
                    [i, key.hex() if key is not None else None, value.hex()]
                )
            src.require(
                self.watermark(consumer, src.HISTORY) == (0, high),
                "HISTORY_UNSTABLE",
            )
            return fingerprint(rows)
        finally:
            consumer.close()

    def durable(self, *, startup=False, expected_lsn=None):
        """read-only RESTのoffsetと稼働状態が揃うまで期限内で待つ。"""
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            try:
                if not self.live_task():
                    time.sleep(1)
                    continue
                pos = saved_snapshot(
                    self.http(f"/connectors/{src.CONNECTOR}/offsets"),
                    expected_lsn=expected_lsn,
                )
                if pos is not None:
                    return pos
            except HTTPError as error:
                if error.code not in src.CONNECT_TRANSIENT and not (
                    startup and error.code == 404
                ):
                    raise src.Failure("SOURCE_OFFSET_REST_FAILED") from None
            except (URLError, TimeoutError, ConnectionError):
                # 既存位置の証明は期限切れなら失敗。推定で無効にしない。
                pass
            time.sleep(1)
        raise src.Failure("SOURCE_OFFSET_NOT_OBSERVED")

    def before(self):
        """初回 SQL fixture の snapshot を検査して停止位置を固める。"""
        self.await_sql()
        self.phase = "RESTART_SQL_PROVISION"
        self.provision()
        self.phase = "RESTART_CONNECT_REGISTER"
        self.register()
        self.phase = "RESTART_BEFORE_STOP"
        snapshot = self.snapshot()
        position = self.durable()
        return {
            "position": position,
            "snapshot": snapshot,
            "history": self.history(),
        }

    def after(self, prior):
        """worker 再起動後も同じ Kafka 状態から新規 I/U/D が流れる。"""
        self.phase = "RESTART_AFTER_START"
        src.require(
            self.durable(startup=True) == prior["position"],
            "SOURCE_OFFSET_CHANGED",
        )
        src.require(
            self.history() == prior["history"], "RESUMED_HISTORY_CHANGED"
        )
        src.require(
            self.snapshot() == prior["snapshot"], "SNAPSHOT_RECORD_CHANGED"
        )
        self.phase = "RESTART_POST_CHANGE"
        consumer = self.consumer()
        try:
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 1)])
            self.offset = 0
            deletion = self.changes(consumer)
            self.verify_final(consumer)
            self.phase = "RESTART_FINAL_OFFSET"
            delivered = src.decode(deletion.value())["source"]["commit_lsn"]
            observed = self.durable(expected_lsn=delivered)
            src.require(
                observed != prior["position"], "FINAL_OFFSET_UNCHANGED"
            )
            src.require(
                self.history() == prior["history"], "FINAL_HISTORY_CHANGED"
            )
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 5),
                "FINAL_DELIVERY_COUNT_CHANGED",
            )
        finally:
            consumer.close()

    def verified_records(self):
        """下流には再生成でなく source が実配送した Kafka message を渡す。"""
        consumer = self.consumer()
        try:
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 5),
                "HANDOFF_INVENTORY",
            )
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 0)])
            for n, op in enumerate(("r", "c", "u", "d", "tombstone")):
                msg = self.message(consumer, src.TOPIC, n)
                src.checkpoint(
                    src.TOPIC, 0, n, n - 1, msg.key(), msg.value(), op
                )
                yield msg
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 5),
                "HANDOFF_CHANGED",
            )
        finally:
            consumer.close()


def run():
    """二プロセス間では所有する専用 bind だけでハッシュを引き継ぐ。"""
    worker = None
    try:
        src.validate_hosted()
        from types import SimpleNamespace

        import pymssql
        from confluent_kafka import Consumer, TopicPartition
        from confluent_kafka.admin import AdminClient

        secret, reader = src.fixture_passwords()
        api = SimpleNamespace(Consumer=Consumer, TopicPartition=TopicPartition)
        worker = Restart(pymssql, api, AdminClient, secret, reader)
        if RECEIPT.is_file() and not RECEIPT.is_symlink():
            with RECEIPT.open(encoding="utf-8") as f:
                prior = validate_receipt(src.decode(f.read(2048).encode()))
            worker.after(prior)
            worker.phase = "RESTART_HANDOFF"
            src.require(
                sum(1 for _ in worker.verified_records()) == 5, "HANDOFF_COUNT"
            )
            print(
                "SQL_SOURCE_RESTART=pass snapshot=1 insert=1 update=1 "
                "delete=1 tombstone=1"
            )
        else:
            prior = worker.before()
            descriptor = os.open(
                RECEIPT,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as f:
                json.dump(prior, f)
                f.flush()
                os.fsync(f.fileno())
            print("SQL_SOURCE_RESTART_BEFORE=pass")
    except src.Failure as error:
        print(
            f"FAIL: {worker.phase if worker else 'RESTART_ENTRY'}/{error}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            f"FAIL: {worker.phase if worker else 'RESTART_ENTRY'}/RUNTIME",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
