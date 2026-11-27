"""使い捨て source が実配送した同一Kafkaレコードで契約→Icebergを検証。"""

import sqlite3
import sys
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import proof as src

SOURCE = Path(__file__).resolve()
ROOT = (
    Path("/checkout")
    if SOURCE.parent == Path("/fixture")
    else SOURCE.parents[3]
)
sys.path.insert(0, str(ROOT / "infra/cdc"))
LANES = ("raw", "current", "silver")
GROUP = "fixture-wire-to-iceberg"
TABLE = {
    "id": "fixture_orders",
    "enabled": True,
    "source": {
        "topic_prefix": src.PREFIX,
        "database": src.DATABASE,
        "schema": "dbo",
        "table": src.TABLE,
    },
    "primary_key": list(src.KEY_FIELDS),
    "mode": "current",
    "delete_policy": "remove",
    "snapshot": "initial",
    "destinations": {
        "raw": {"namespace": "wire", "table": "raw", "stream": "orders"},
        "current": {"namespace": "wire", "table": "current"},
        "silver": {"namespace": "wire", "table": "silver"},
    },
    "columns": [
        {
            "name": "site_id",
            "type": "nvarchar",
            "width": 20,
            "nullable": False,
        },
        {"name": "record_id", "type": "int", "nullable": False},
        {"name": "label", "type": "nvarchar", "width": 40, "nullable": True},
        {
            "name": "quantity",
            "type": "decimal",
            "precision": 18,
            "scale": 3,
            "nullable": False,
        },
        {
            "name": "observed_at",
            "type": "datetime2",
            "scale": 7,
            "nullable": False,
        },
    ],
}


def schema(lane):
    """7桁時刻は文字列で、decimal は同じscaleの物理型で維持する。"""
    import pyarrow as pa

    fields = [pa.field("offset", pa.int64(), nullable=False)]
    if lane == "raw":
        return pa.schema(
            fields
            + [
                pa.field("key_json", pa.string(), nullable=False),
                pa.field("value_json", pa.string()),
            ]
        )
    return pa.schema(
        fields
        + [
            pa.field("site_id", pa.string(), nullable=False),
            pa.field("record_id", pa.int32(), nullable=False),
            pa.field("label", pa.string()),
            pa.field("quantity", pa.decimal128(18, 3), nullable=False),
            pa.field("observed_at", pa.string(), nullable=False),
        ]
    )


def expected(offset):
    """消費側codecを使わず、元の行と別行のI/U/D後の行集合を定義。"""
    survivor = dict(
        offset=offset,
        site_id="sample",
        record_id=1,
        label=None,
        quantity=Decimal("2.500"),
        observed_at=src.STAMP0,
    )
    rows = [survivor]
    if offset in {1, 2}:
        rows.append(
            dict(
                survivor,
                record_id=2,
                label=None if offset == 1 else "",
                quantity=Decimal("12.340" if offset == 1 else "-1.234"),
                observed_at=src.STAMP1 if offset == 1 else src.STAMP2,
            )
        )
    return rows


def ordered(rows):
    """物理ファイル順に依存せず、重複を捨てずに比較する。"""
    return sorted(
        rows, key=lambda row: (row["offset"], row.get("record_id", 0))
    )


def read(catalog, lane, offset=None):
    """各検査で永続メタデータとParquetから読み直す。"""
    from pyiceberg.expressions import EqualTo

    table = catalog.load_table(("wire", lane))
    scan = (
        table.scan()
        if offset is None
        else table.scan(row_filter=EqualTo("offset", offset))
    )
    return ordered(scan.to_arrow().to_pylist())


def ensure(catalog, lane, offset, rows):
    """世代全体を1 commitで保存し、既存が厳密一致なら二重追加しない。"""
    import pyarrow as pa

    existing = read(catalog, lane, offset)
    if not existing:
        table = catalog.load_table(("wire", lane))
        table.append(pa.Table.from_pylist(rows, schema=schema(lane)))
    src.require(
        read(catalog, lane, offset) == ordered(rows), "ICEBERG_READBACK"
    )


def store(root, existing):
    """カタログも状態ファイルも実体を使い、再開時は空を作らない。"""
    from pyiceberg.catalog.sql import SqlCatalog

    position = root / "state.db"
    catalog_file = root / "catalog.db"
    src.require(
        all(
            path.is_file() and not path.is_symlink()
            for path in (position, catalog_file)
        )
        if existing
        else not any(root.iterdir()),
        "CHAIN_STATE_IDENTITY",
    )
    catalog = SqlCatalog(
        "wire-proof",
        uri=f"sqlite:///{catalog_file}",
        warehouse=(root / "warehouse").as_uri(),
        **{"py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO"},
    )
    try:
        if not existing:
            catalog.create_namespace("wire")
            for lane in LANES:
                catalog.create_table(("wire", lane), schema(lane))
        src.require(
            set(catalog.list_tables("wire"))
            == {("wire", lane) for lane in LANES},
            "CHAIN_CATALOG_IDENTITY",
        )
        return catalog
    except Exception:
        catalog.engine.dispose()
        raise


def apply(experiment, catalog, record):
    """本物の consumer.process hook で Raw、型付き世代、ローカル位置を確定。"""
    from consumer import process

    offset = record["offset"]
    raw = {
        "offset": offset,
        "key_json": record["key"],
        "value_json": record["value"],
    }

    def hook(_admin, _state, records, _mapping, _topics, *, values):
        """隔離試験だけの storage adapter。通常の入口は差し替えない。"""
        src.require(
            records == [record] and values is None, "CHAIN_BATCH_SHAPE"
        )
        ensure(catalog, "raw", offset, [raw])

        def typed_materialization():
            for lane in LANES[1:]:
                rows = []
                for stored in experiment.rows(TABLE["id"], layer=lane):
                    typed = dict(stored, offset=offset)
                    typed["quantity"] = Decimal(typed["quantity"])
                    rows.append(typed)
                src.require(
                    ordered(rows) == expected(offset), "CHAIN_TYPED_ROWS"
                )
                ensure(catalog, lane, offset, rows)

        return experiment.process(
            records, fail_materialization=typed_materialization
        )

    process(None, None, [record], {}, {}, contract_hook=hook)
    src.require(
        experiment.offset(src.TOPIC, 0) == offset, "CHAIN_LOCAL_POSITION"
    )
    return raw


def committed(consumer, coords, expected_offset):
    """Kafka commit はメモリカーソルでなく group の応答から別に観測する。"""
    result = consumer.committed([coords], timeout=8)
    src.require(
        isinstance(result, list)
        and len(result) == 1
        and (
            result[0].topic,
            result[0].partition,
            result[0].offset,
            result[0].error,
        )
        == (src.TOPIC, 0, expected_offset, None),
        "CHAIN_GROUP_OFFSET",
    )


def same_wire(message, handoff):
    """検証済みsourceのbytesとconsumerが実際に受信したbytesを結ぶ。"""
    src.require(
        (message.key(), message.value()) == handoff, "CHAIN_WIRE_CHANGED"
    )


def run(source, root, handoff):
    """元のbrokerを読み、snapshot後にconsumer/カタログ/状態を再openする。"""
    from confluent_kafka import OFFSET_INVALID, Consumer, TopicPartition
    from consumer import message_record
    from contract_experiment import Experiment

    src.require(len(handoff) == 5, "CHAIN_HANDOFF_COUNT")
    coord = TopicPartition(src.TOPIC, 0)
    doc = {"version": 1, "tables": [TABLE]}
    receipt = []
    for start, end in ((0, 1), (1, 5)):
        catalog = store(root, start != 0)
        consumer = Consumer(
            {
                "bootstrap.servers": src.BROKER,
                "group.id": GROUP,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "allow.auto.create.topics": False,
                "log_level": 0,
            }
        )
        sqlite_url = (root / "state.db").as_uri()
        try:
            with closing(
                sqlite3.connect(
                    sqlite_url + ("?mode=rw" if start else "?mode=rwc"),
                    uri=True,
                )
            ) as db:
                db.execute("PRAGMA synchronous=FULL")
                experiment = Experiment(doc, db)
                src.require(
                    experiment.offset(src.TOPIC, 0) == start - 1,
                    "CHAIN_RESUME_LOCAL",
                )
                committed(consumer, coord, start if start else OFFSET_INVALID)
                consumer.assign([TopicPartition(src.TOPIC, 0, start)])
                for n in range(start, end):
                    operation = ("r", "c", "u", "d", "tombstone")[n]
                    message = source.message(consumer, src.TOPIC, n)
                    src.checkpoint(
                        src.TOPIC,
                        0,
                        n,
                        n - 1,
                        message.key(),
                        message.value(),
                        operation,
                    )
                    same_wire(message, handoff[n])
                    record = message_record(message)
                    receipt.append(apply(experiment, catalog, record))
                    with closing(
                        sqlite3.connect(sqlite_url + "?mode=ro", uri=True)
                    ) as readonly:
                        row = readonly.execute(
                            "SELECT off FROM checkpoints "
                            "WHERE topic=? AND part=0",
                            (src.TOPIC,),
                        ).fetchall()
                    src.require(row == [(n,)], "CHAIN_DURABLE_BEFORE_ACK")
                    result = consumer.commit(
                        offsets=[TopicPartition(src.TOPIC, 0, n + 1)],
                        asynchronous=False,
                    )
                    src.require(
                        isinstance(result, list)
                        and len(result) == 1
                        and (
                            result[0].topic,
                            result[0].partition,
                            result[0].offset,
                            result[0].error,
                        )
                        == (src.TOPIC, 0, n + 1, None),
                        "CHAIN_KAFKA_ACK",
                    )
                committed(consumer, coord, end)
                src.require(
                    experiment.raw_count() == end
                    and experiment.quarantine_count() == 0
                    and read(catalog, "raw") == receipt,
                    "CHAIN_RAW_EXACT",
                )
                expected_all = [
                    row for offset in range(end) for row in expected(offset)
                ]
                for lane in LANES[1:]:
                    src.require(
                        read(catalog, lane) == expected_all,
                        "CHAIN_GENERATIONS",
                    )
                if end == 5:
                    with closing(experiment.replay().db) as replayed:
                        count = replayed.execute(
                            "SELECT count(*) FROM accepted"
                        ).fetchone()
                    src.require(count == (5,), "CHAIN_REPLAY_COUNT")
        finally:
            consumer.close()
            catalog.engine.dispose()
