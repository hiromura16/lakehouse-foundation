"""同じ合成 SQL source worker を停止・再始動した前後の永続境界。"""

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import HTTPError, URLError

import proof as src

RECEIPT = Path("/restart-state/restart.json")
STOPPED = Path("/restart-state/stopped.json")
LSN = re.compile(r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}:[0-9a-fA-F]{4}")
SHA = re.compile(r"[0-9a-f]{64}")
MAX_HISTORY = 64


def fingerprint(value):
    """状態の値そのものを runner レシートや公開ログへ移さない。"""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def observed_offset(body, *, expected_lsn=None):
    """Kafka Connectの永続位置を、既知のSQL Server形式だけで識別する。"""
    entries = body.get("offsets") if isinstance(body, dict) else None
    if entries == []:
        return None, "EMPTY"
    if not isinstance(entries, list) or len(entries) != 1:
        return None, "COUNT"
    entry = entries[0]
    if not isinstance(entry, dict):
        return None, "ENTRY"
    partition = entry.get("partition")
    if partition != {"server": src.PREFIX, "database": src.DATABASE}:
        return None, "PARTITION"
    position = entry.get("offset")
    if not isinstance(position, dict):
        return None, "POSITION"
    # いずれのsnapshot表現も、今回要求するstreaming位置の証拠ではない。
    if "snapshot" in position or position.get("snapshot_completed") is False:
        return None, "SNAPSHOT_PHASE"
    commit_lsn = position.get("commit_lsn")
    if (
        type(commit_lsn) is not str
        or not LSN.fullmatch(commit_lsn)
        or set(commit_lsn) <= {"0", ":"}
    ):
        return None, "COMMIT_LSN"
    if expected_lsn is not None and commit_lsn.lower() != expected_lsn.lower():
        return None, "EXPECTED_LSN_NOT_COMMITTED"
    change_lsn = position.get("change_lsn")
    serial = position.get("event_serial_no")
    command = position.get("command_id")
    if (
        type(change_lsn) is not str
        or not LSN.fullmatch(change_lsn)
        or set(change_lsn) <= {"0", ":"}
    ):
        return None, "CHANGE_LSN"
    if type(serial) is not int or serial < 0:
        return None, "SERIAL"
    if command is not None and (type(command) is not int or command < 0):
        return None, "COMMAND"
    change_lsn = change_lsn.lower()
    digest = fingerprint(
        [partition, commit_lsn.lower(), change_lsn, serial, command]
    )
    return digest, "VALID"


def saved_stream_position(body, *, expected_lsn=None):
    """試験で比較する確定位置だけを返す。"""
    return observed_offset(body, expected_lsn=expected_lsn)[0]


def validate_receipt(document):
    """存在だけでは成功扱いせず、先行証跡は固定形のみ受理する。"""
    src.require(
        isinstance(document, dict)
        and set(document) == {"position", "history", "prefix"}
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

    @staticmethod
    def prefix_digest(messages):
        """最初の2件のバイト列を順序と別々のキー・値で結び付ける。"""
        return fingerprint(
            [[msg.key().hex(), msg.value().hex()] for msg in messages]
        )

    def before_committed_insert(self):
        """snapshotの真正な配信後に別行をINSERTし、配信を検査する。"""
        consumer = self.consumer()
        try:
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 0)])
            initial = self.evidence(consumer, "r")
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 1),
                "EXPECTED_ONE_SNAPSHOT",
            )
            self.phase = "RESTART_BEFORE_INSERT"
            self.insert(2, "12.340", src.STAMP1)
            insert = self.evidence(consumer, "c")
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 2),
                "EXPECTED_SNAPSHOT_AND_INSERT",
            )
            lsn = src.decode(insert.value())["source"]["commit_lsn"]
            return self.prefix_digest([initial, insert]), lsn
        finally:
            consumer.close()

    def retained_prefix(self):
        """restart後に同じbrokerからrとcを元のoffsetで再読する。"""
        consumer = self.consumer()
        try:
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 0)])
            prefix = []
            for offset, op in enumerate(("r", "c")):
                msg = self.message(consumer, src.TOPIC, offset)
                src.checkpoint(
                    src.TOPIC,
                    0,
                    offset,
                    offset - 1,
                    msg.key(),
                    msg.value(),
                    op,
                )
                prefix.append(msg)
            src.require(
                self.watermark(consumer, src.TOPIC) in {(0, 2), (0, 3)},
                "UNEXPECTED_DELIVERY_AFTER_RESTART",
            )
            return self.prefix_digest(prefix)
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
        reason = "NOT_READY"
        while time.monotonic() < deadline:
            try:
                if not self.live_task():
                    time.sleep(1)
                    continue
                pos, reason = observed_offset(
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
                reason = f"HTTP_{error.code}"
            except (URLError, TimeoutError, ConnectionError):
                reason = "TRANSPORT"
            time.sleep(1)
        raise src.Failure(f"SOURCE_OFFSET_NOT_OBSERVED_{reason}")

    def captured_seed(self):
        """SQL Agentが初期行を実際にCDC捕捉するまでsnapshotを始めない。"""
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            rows = self.execute(
                src.DATABASE,
                "SELECT TOP (2) 1 FROM cdc.dbo_fixture_orders_CT "
                "WHERE [__$operation]=2 AND site_id=N'sample' AND record_id=1 "
                "AND [__$start_lsn] IS NOT NULL "
                "AND [__$start_lsn]<>0x00000000000000000000",
                reader=True,
            )
            if rows:
                src.require(rows == [(1,)], "NON_UNIQUE_CAPTURED_SEED")
                return
            time.sleep(1)
        raise src.Failure("ACTUAL_SEED_CAPTURE_NOT_OBSERVED")

    def before(self):
        """snapshot後の実INSERTの保存済みCDC位置でのみ停止を許す。"""
        self.await_sql()
        self.phase = "RESTART_SQL_PROVISION"
        self.provision()
        self.phase = "RESTART_WAIT_CAPTURED_SEED"
        self.captured_seed()
        self.phase = "RESTART_CONNECT_REGISTER"
        self.register()
        self.phase = "RESTART_BEFORE_STOP"
        prefix, lsn = self.before_committed_insert()
        self.phase = "RESTART_BEFORE_STOP_STREAM_OFFSET"
        position = self.durable(expected_lsn=lsn)
        return {
            "position": position,
            "prefix": prefix,
            "history": self.history(),
        }

    def stopped_update(self):
        """Connect停止中の1件を、SQL現在値・CDC実捕捉・Kafka不変で証明。"""
        self.phase = "STOPPED_SQL_UPDATE"
        consumer = self.consumer()
        try:
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 2),
                "STOPPED_KAFKA_PREFIX_BEFORE",
            )
            self.verify_order(before=True)
            self.update_target()
            self.verify_order()
            self.phase = "STOPPED_CDC_CAPTURE"
            update_position = self.update_capture()
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 2),
                "STOPPED_KAFKA_ADVANCED",
            )
            return fingerprint(update_position)
        finally:
            consumer.close()

    def verify_order(self, *, before=False):
        """roundingを避け、SQL内で正確な業務セル値を比較する。"""
        predicate = "label IS NOT NULL AND DATALENGTH(label)=0"
        quantity, stamp = "-1.234", src.STAMP2
        if before:
            predicate = "label IS NULL"
            quantity, stamp = "12.340", src.STAMP1
        result = self.execute(
            src.DATABASE,
            "SELECT COUNT_BIG(*) FROM dbo.fixture_orders "
            "WHERE site_id=N'sample' AND record_id=2 AND "
            + predicate
            + " AND quantity=CONVERT(decimal(18,3),%s) "
            "AND observed_at=CONVERT(datetime2(7),%s)",
            (quantity, stamp),
            reader=True,
        )
        src.require(result == [(1,)], "STOPPED_SQL_ROW_WRONG")

    def update_capture(self):
        """実SQL CDC change tableの更新後像からcommit LSNを把握する。"""
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            rows = self.execute(
                src.DATABASE,
                "SELECT TOP (2) [__$start_lsn] FROM cdc.dbo_fixture_orders_CT "
                "WHERE [__$operation]=4 AND site_id=N'sample' AND record_id=2 "
                "AND label IS NOT NULL AND DATALENGTH(label)=0 "
                "AND quantity=CONVERT(decimal(18,3),%s) "
                "AND observed_at=CONVERT(datetime2(7),%s)",
                ("-1.234", src.STAMP2),
                reader=True,
            )
            if rows:
                src.require(len(rows) == 1, "STOPPED_CDC_UNEXPECTED_COUNT")
                binary_lsn = rows[0][0]
                src.require(
                    type(binary_lsn) in {bytes, bytearray}
                    and len(binary_lsn) == 10
                    and any(binary_lsn),
                    "STOPPED_CDC_INVALID_LSN",
                )
                hex_lsn = binary_lsn.hex()
                return ":".join((hex_lsn[:8], hex_lsn[8:16], hex_lsn[16:]))
            time.sleep(1)
        raise src.Failure("STOPPED_CDC_CAPTURE_NOT_OBSERVED")

    def after(self, prior, stopped_lsn_digest):
        """停止中に確定したSQL UPDATEと同じものだけをKafkaで受理する。"""
        self.phase = "RESTART_AFTER_START"
        self.connect_identity()
        startup_position = self.durable(startup=True)
        src.require(
            self.history() == prior["history"], "RESUMED_HISTORY_CHANGED"
        )
        src.require(
            self.retained_prefix() == prior["prefix"], "PREFIX_RECORDS_CHANGED"
        )
        self.phase = "RESTART_CATCHUP_KAFKA"
        consumer = self.consumer()
        try:
            consumer.assign([self.kafka.TopicPartition(src.TOPIC, 0, 2)])
            self.offset = 1
            update = self.evidence(consumer, "u")
            caught = src.decode(update.value())["source"]["commit_lsn"]
            src.require(
                fingerprint(caught.lower()) == stopped_lsn_digest,
                "STOPPED_SQL_KAFKA_LSN_MISMATCH",
            )
            stopped_position = self.durable(expected_lsn=caught)
            src.require(
                stopped_position != prior["position"]
                and startup_position in {prior["position"], stopped_position},
                "UNEXPECTED_RESTART_POSITION",
            )
            src.require(
                self.watermark(consumer, src.TOPIC) == (0, 3),
                "UNEXPECTED_CATCHUP_DELIVERY_COUNT",
            )
            print("SQL_SOURCE_STOPPED_KAFKA_CATCHUP=pass", flush=True)
            deletion = self.delete_target(consumer)
            self.verify_final(consumer)
            self.phase = "RESTART_FINAL_OFFSET"
            delivered = src.decode(deletion.value())["source"]["commit_lsn"]
            observed = self.durable(expected_lsn=delivered)
            src.require(
                observed not in {prior["position"], stopped_position},
                "FINAL_OFFSET_UNCHANGED",
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


def write_receipt(path, content):
    """専用scratchの新規証跡だけを排他作成する。"""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(content, output)
        output.flush()
        os.fsync(output.fileno())


def read_receipt(path):
    """証跡を短い固定形式としてだけ読む。"""
    src.require(
        path.is_file() and not path.is_symlink(), "UNSAFE_FIXTURE_RECEIPT"
    )
    with path.open(encoding="utf-8") as source:
        return src.decode(source.read(2048).encode())


def run():
    """三段階のプロセス間で専用bindとハッシュだけを引き継ぐ。"""
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
        if STOPPED.exists():
            prior = validate_receipt(read_receipt(RECEIPT))
            lsn_digest = read_receipt(STOPPED)
            src.require(
                type(lsn_digest) is str and SHA.fullmatch(lsn_digest),
                "INVALID_STOPPED_RECEIPT",
            )
            worker.after(prior, lsn_digest)
            worker.phase = "RESTART_HANDOFF"
            with_source = tuple(
                (msg.key(), msg.value()) for msg in worker.verified_records()
            )
            src.require(len(with_source) == 5, "HANDOFF_COUNT")
            print(
                "SQL_SOURCE_STOPPED_EXACT_SEQUENCE=pass records=5", flush=True
            )
            if os.environ.get("CDC_SQL_SOURCE_CHAIN") == "true":
                print("SQL_WIRE_SOURCE_HANDOFF=pass records=5", flush=True)
                worker.phase = "RESTART_ICEBERG_CHAIN"
                import wire_iceberg

                with TemporaryDirectory(prefix="wire-iceberg-") as workspace:
                    wire_iceberg.run(worker, Path(workspace), with_source)
                print(
                    "SQL_WIRE_NATIVE_EVIDENCE=pass snapshot_lanes=3 "
                    "typed_final=1 deleted=1 lag=0 mismatches=0",
                    flush=True,
                )
                print(
                    "SQL_WIRE_ICEBERG=pass raw=5 current_generations=5 "
                    "silver_generations=5 broker_next=5"
                )
            else:
                print(
                    "SQL_SOURCE_RESTART=pass snapshot=1 insert=1 update=1 "
                    "delete=1 tombstone=1"
                )
        elif RECEIPT.exists():
            validate_receipt(read_receipt(RECEIPT))
            write_receipt(STOPPED, worker.stopped_update())
            print("SQL_SOURCE_STOPPED_DML=pass kafka_high=2")
        else:
            write_receipt(RECEIPT, worker.before())
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
