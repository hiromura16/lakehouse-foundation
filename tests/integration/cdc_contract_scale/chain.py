"""合成5表の実配送から実保存とACKへの一方向の引き渡し。

既存ランナーを起動しない。呼出側は同じ閉鎖broker、同じ受信client、
排他的に所有し未commitのfixture groupを提供すること。Python側の
型やorigin検査は、この運用境界や受信の真正性を認証しない。
"""

import re
from dataclasses import dataclass

import fixture
import source
import storage

GROUP = source.CHAIN_GROUP
OFFSET_INVALID = -1001
WANTED = {
    spec.topic: spec.initial + 5 * spec.cycles for spec in fixture.TABLES
}
LIMIT = sum(WANTED.values())
RECORD_FIELDS = {"topic", "partition", "offset", "key", "value", "ingested_at"}


@dataclass(frozen=True, slots=True)
class Delivery:
    """原packetから読んだUTF-8のkey/value等を、そのまま保持する。"""

    topic: str
    partition: int
    offset: int
    key: str
    value: str | None
    ingested_at: str

    def as_record(self):
        """キー/値を再生成せず、Consumer用の新しいdictに結ぶ。"""
        return {
            "topic": self.topic,
            "partition": self.partition,
            "offset": self.offset,
            "key": self.key,
            "value": self.value,
            "ingested_at": self.ingested_at,
        }


@dataclass(frozen=True, slots=True)
class Handoff:
    """所有済みscopeと記録されたbrokerの次位置を一緒に渡す。"""

    cluster: str
    group: str
    source_watermarks: tuple
    records: tuple


def scope(cluster, group):
    """既存の合成groupとrunner発行clusterの型だけを扱う。"""
    fixture.demand(
        isinstance(cluster, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{22}", cluster) is not None
        and type(group) is str
        and group == GROUP,
        "CHAIN_SOURCE_SCOPE",
    )


def coordinates(value):
    """全五表のwatermarkが余分・欠落なしで契約と同じかを確認する。"""
    fixture.demand(
        isinstance(value, dict)
        and set(value) == set(WANTED)
        and all(
            type(row) is tuple
            and len(row) == 2
            and all(type(offset) is int for offset in row)
            and row == (0, WANTED[topic])
            for topic, row in value.items()
        ),
        "CHAIN_SOURCE_WATERMARK",
    )
    return tuple((topic, *value[topic]) for topic in sorted(value))


def validate_sealed_watermarks(value):
    """手渡し型が直接生成された場合も、bool等を境界値にしない。"""
    fixture.demand(
        type(value) is tuple
        and len(value) == len(WANTED)
        and all(
            type(row) is tuple and len(row) == 3 and type(row[0]) is str
            for row in value
        ),
        "CHAIN_HANDOFF_WATERMARK",
    )
    rebuilt = coordinates({topic: (low, high) for topic, low, high in value})
    fixture.demand(rebuilt == value, "CHAIN_HANDOFF_WATERMARK")


def validate_originals(records):
    """型・origin・完全な各表順を再検査。代替wireを作らない。"""
    from contract_experiment import Experiment

    fixture.demand(
        isinstance(records, (tuple, list)) and len(records) == LIMIT,
        "CHAIN_RECORD_COUNT",
    )
    by_topic = source.plans()
    spec = {member.topic: member for member in fixture.TABLES}
    next_offset = dict.fromkeys(WANTED, 0)
    result = []
    snapshot_records = []
    for record in records:
        fixture.demand(
            isinstance(record, dict) and set(record) == RECORD_FIELDS,
            "CHAIN_RECORD_SHAPE",
        )
        topic, _, offset = Experiment.delivery(record)
        fixture.demand(
            topic in WANTED
            and offset == next_offset[topic]
            and offset < WANTED[topic],
            "CHAIN_SOURCE_POSITION",
        )
        expected = by_topic[topic][offset]
        fixture.verify_record(record, spec[topic], offset, expected)
        try:
            source.verify_origin(
                record,
                expected.op,
                position=offset,
                table_size=spec[topic].initial,
            )
        except source.base.Failure:
            raise ValueError("CHAIN_NATIVE_ORIGIN") from None
        result.append(Delivery(**record))
        if expected.op == "r":
            snapshot_records.append(record)
        next_offset[topic] += 1
    fixture.demand(next_offset == WANTED, "CHAIN_SOURCE_COUNT")
    try:
        source.verify_snapshot_fence(snapshot_records)
    except source.base.Failure:
        raise ValueError("CHAIN_NATIVE_SNAPSHOT_FENCE") from None
    return tuple(result)


def capture(records, watermarks, *, cluster, group):
    """既存ScaleからSQL/model検証済みの元配送を受け取るためのseam。"""
    scope(cluster, group)
    return Handoff(
        cluster, group, coordinates(watermarks), validate_originals(records)
    )


def port_identity(port, handoff):
    """所有済みportの観測値を使い、別cluster/groupでは何もしない。"""
    fixture.demand(
        port.identity() == (handoff.cluster, handoff.group),
        "CHAIN_PORT_IDENTITY",
    )


def port_highs(port):
    """cached値を避け、同じ全topic/partitionのlow/highを再確認。"""
    result = {
        topic: port.client.get_watermark_offsets(
            port.topic_partition(topic, 0), timeout=5, cached=False
        )
        for topic in sorted(WANTED)
    }
    return coordinates(result)


def group_positions(receipt, *, expected, code):
    """Kafka戻り値のtopic/partition/error/nextを個別に厳密確認。"""
    fixture.demand(
        isinstance(receipt, list) and len(receipt) == len(WANTED), code
    )
    result = {}
    for item in receipt:
        topic = getattr(item, "topic", None)
        fixture.demand(
            isinstance(topic, str)
            and topic in WANTED
            and topic not in result
            and type(getattr(item, "partition", None)) is int
            and item.partition == 0
            and getattr(item, "error", True) is None
            and type(getattr(item, "offset", None)) is int
            and item.offset == expected[topic],
            code,
        )
        result[topic] = item.offset
    fixture.demand(result == expected, code)
    return result


def require_uncommitted(port):
    """単にgroupのhighが一致するだけでなく、開始時の未commitを照合。"""
    coords = [port.topic_partition(topic, 0) for topic in sorted(WANTED)]
    receipt = port.client.committed(coords, timeout=8)
    group_positions(
        receipt,
        expected=dict.fromkeys(WANTED, OFFSET_INVALID),
        code="CHAIN_GROUP_NOT_FRESH",
    )


def run(handoff, port, *, store_factory=storage.Harness):
    """保存/読戻しと全位置の一致後、一度だけ同期commitを要求する。

    port は memoized high ではなく同じConsumerの新規観測、かつその
    Consumerの実groupを生成時から束縛したfixture-only wrapperとする。
    commitの応答が不明/部分成功の場合、失敗を通知し再試行はしない。
    KafkaとSQLite/Icebergの分散transactionやCASを主張しない。
    """
    fixture.demand(type(handoff) is Handoff, "CHAIN_HANDOFF_SHAPE")
    scope(handoff.cluster, handoff.group)
    fixture.demand(
        isinstance(handoff.records, tuple)
        and len(handoff.records) == LIMIT
        and all(type(row) is Delivery for row in handoff.records),
        "CHAIN_HANDOFF_SHAPE",
    )
    validate_sealed_watermarks(handoff.source_watermarks)
    original = [row.as_record() for row in handoff.records]
    fixture.demand(
        validate_originals(original) == handoff.records,
        "CHAIN_HANDOFF_CHANGED",
    )
    port_identity(port, handoff)
    fixture.demand(
        port_highs(port) == handoff.source_watermarks,
        "CHAIN_HANDOFF_WATERMARK",
    )
    require_uncommitted(port)
    with store_factory() as sink:
        fixture.demand(sink.offsets() == {}, "CHAIN_STORAGE_NOT_FRESH")
        for start in range(0, LIMIT, storage.MAX_BATCH):
            end = min(start + storage.MAX_BATCH, LIMIT)
            window = slice(start, end)
            receipt = sink.write(original[window])
            fixture.demand(
                type(receipt.get("accepted")) is int
                and receipt["accepted"] == end - start,
                "CHAIN_STORAGE_ACCEPTANCE",
            )
        verdict = sink.verify_final(original)
        fixture.demand(
            verdict["raw_rows"] == LIMIT
            and verdict["current_rows"] == 112
            and verdict["silver_rows"] == 112,
            "CHAIN_STORAGE_FINAL_COUNTS",
        )
        sink.reopen()
        fixture.demand(
            sink.verify_final(original) == verdict,
            "CHAIN_STORAGE_REOPEN_MISMATCH",
        )
        local = sink.offsets()
        fixture.demand(
            set(local) == set(WANTED)
            and all(
                type(offset) is int and offset + 1 == WANTED[topic]
                for topic, offset in local.items()
            ),
            "CHAIN_LOCAL_NOT_COMPLETE",
        )
        port_identity(port, handoff)
        fixture.demand(
            port_highs(port) == handoff.source_watermarks,
            "CHAIN_HANDOFF_WATERMARK",
        )
        require_uncommitted(port)
        pos = [
            port.topic_partition(topic, 0, WANTED[topic])
            for topic in sorted(WANTED)
        ]
        try:
            ack = port.client.commit(offsets=pos, asynchronous=False)
            group_positions(ack, expected=WANTED, code="CHAIN_ACK_RESPONSE")
            coords = [
                port.topic_partition(topic, 0) for topic in sorted(WANTED)
            ]
            checked = group_positions(
                port.client.committed(coords, timeout=8),
                expected=WANTED,
                code="CHAIN_ACK_READBACK",
            )
            port_identity(port, handoff)
            latest = port_highs(port)
            outstanding = sum(
                high - checked[topic] for topic, _, high in latest
            )
            fixture.demand(outstanding == 0, "CHAIN_OUTSTANDING")
        except Exception:
            # After the commit call starts, partial/unknown success is unsafe
            # to treat as no ACK. Require a fresh operator observation.
            raise ValueError("CHAIN_ACK_UNCERTAIN") from None
        return {
            "source_records": LIMIT,
            "local_partitions": len(local),
            "source_partitions": len(checked),
            "outstanding": outstanding,
            "raw_rows": verdict["raw_rows"],
            "current_rows": verdict["current_rows"],
            "silver_rows": verdict["silver_rows"],
        }


def native(records, watermarks, port):
    """entry pointから渡った実所有consumerだけで保存とACKを実行する。"""
    fixture.demand(type(port) is source.NativePort, "CHAIN_NATIVE_PORT")
    try:
        cluster, group = port.identity()
        handoff = capture(records, watermarks, cluster=cluster, group=group)
        return run(handoff, port)
    except ValueError as error:
        # Error bodies from libraries are not published. ACK uncertainty has
        # its own closed operational result, with no automatic retry.
        code = (
            "SCALE_CHAIN_ACK_UNCERTAIN"
            if error.args == ("CHAIN_ACK_UNCERTAIN",)
            else "SCALE_CHAIN_VERIFY_FAILED"
        )
        raise source.base.Failure(code) from None
