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


def read(catalog, lane, offset=None, *, snapshot_id=None):
    """任意の実snapshot IDをpinし、永続メタデータとParquetを再読する。"""
    from pyiceberg.expressions import EqualTo

    table = catalog.load_table(("wire", lane))
    options = {"snapshot_id": snapshot_id}
    if offset is not None:
        options["row_filter"] = EqualTo("offset", offset)
    return ordered(table.scan(**options).to_arrow().to_pylist())


def snapshot_id(catalog, lane, *, empty=False):
    """データの件数から推測せず、実Iceberg snapshot pointerを読む。"""
    current = catalog.load_table(("wire", lane)).current_snapshot()
    if current is None and empty:
        return None
    src.require(
        current is not None
        and type(current.snapshot_id) is int
        and current.snapshot_id > 0,
        "CHAIN_ICEBERG_SNAPSHOT_ID",
    )
    return current.snapshot_id


def snapshots(catalog):
    """異なる3テーブルそれぞれの実IDを戻す。値は公開ログに出さない。"""
    return {lane: snapshot_id(catalog, lane) for lane in LANES}


def ensure(catalog, lane, offset, rows):
    """世代全体の実appendとsnapshot lineage、同世代の再読を検証。"""
    import pyarrow as pa

    previous = snapshot_id(catalog, lane, empty=True)
    existing = read(catalog, lane, offset, snapshot_id=previous)
    if not existing:
        table = catalog.load_table(("wire", lane))
        table.append(pa.Table.from_pylist(rows, schema=schema(lane)))
        current = snapshot_id(catalog, lane)
        committed_snapshot = catalog.load_table(
            ("wire", lane)
        ).current_snapshot()
        src.require(
            current != previous
            and committed_snapshot.parent_snapshot_id == previous,
            "CHAIN_ICEBERG_LINEAGE",
        )
    else:
        current = previous
        src.require(
            current is not None and snapshot_id(catalog, lane) == current,
            "CHAIN_ICEBERG_UNEXPECTED_ADVANCE",
        )
    src.require(
        read(catalog, lane, offset, snapshot_id=current) == ordered(rows),
        "ICEBERG_READBACK",
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


def reopened_snapshots(catalog, witnessed, raw):
    """再openで同じsnapshot IDsとfirst generationの実行が維持されたか。"""
    src.require(snapshots(catalog) == witnessed, "CHAIN_REOPEN_SNAPSHOT_IDS")
    check_snapshot_rows(catalog, witnessed, raw)


def check_snapshot_rows(catalog, witnessed, raw):
    """現在のpointerに頼らず、指定したsnapshotの内容を比較する。"""
    src.require(
        read(catalog, "raw", snapshot_id=witnessed["raw"]) == raw[:1],
        "CHAIN_REOPEN_RAW_SNAPSHOT",
    )
    for lane in LANES[1:]:
        src.require(
            read(catalog, lane, snapshot_id=witnessed[lane]) == expected(0),
            "CHAIN_REOPEN_DERIVED_SNAPSHOT",
        )


def read_final_sql(source, catalog, ids):
    """最終時点のSQL保持行全型と削除先キーを実照会し、両派生層と比較。"""
    rows = source.execute(
        src.DATABASE,
        "SELECT site_id,record_id,label,quantity,"
        "CONVERT(varchar(27),observed_at,126) "
        "FROM dbo.fixture_orders ORDER BY site_id,record_id",
        reader=True,
    )
    src.require(
        len(rows) == 1
        and len(rows[0]) == 5
        and type(rows[0][0]) is str
        and type(rows[0][1]) is int
        and rows[0][2] is None
        and type(rows[0][3]) is Decimal
        and rows[0][3].as_tuple().exponent == -3
        and type(rows[0][4]) is str,
        "CHAIN_SQL_FINAL_TYPED_SHAPE",
    )
    kept = dict(
        zip(
            ("site_id", "record_id", "label", "quantity", "observed_at"),
            rows[0],
            strict=True,
        )
    )
    kept["offset"] = 4
    src.require([kept] == expected(4), "CHAIN_SQL_FINAL_EXPECTED")
    dropped = source.execute(
        src.DATABASE,
        "SELECT COUNT_BIG(*) FROM dbo.fixture_orders "
        "WHERE site_id=N'sample' AND record_id=2",
        reader=True,
    )
    src.require(dropped == [(0,)], "CHAIN_SQL_DELETED_KEY_PRESENT")
    for lane in LANES[1:]:
        src.require(
            read(catalog, lane, 4, snapshot_id=ids[lane]) == [kept],
            "CHAIN_SQL_ICEBERG_FINAL_MISMATCH",
        )


def local_position(sqlite_url):
    """別のread-only SQLite接続から正確な完了位置を読む。"""
    with closing(sqlite3.connect(sqlite_url + "?mode=ro", uri=True)) as reader:
        rows = reader.execute(
            "SELECT off FROM checkpoints WHERE topic=? AND part=0",
            (src.TOPIC,),
        ).fetchall()
    src.require(
        len(rows) == 1 and len(rows[0]) == 1 and type(rows[0][0]) is int,
        "CHAIN_LOCAL_POSITION_NOT_OBSERVED",
    )
    return rows[0][0]


def verify_final_lag(source, consumer, coords, sqlite_url, experiment):
    """latest/high, durable SQLiteとKafka groupを別々に読んで0を確認。"""
    low, high = source.watermark(consumer, src.TOPIC)
    local_next = local_position(sqlite_url) + 1
    src.require(
        (low, high) == (0, 5)
        and local_next == high
        and experiment.offset(src.TOPIC, 0) + 1 == high,
        "CHAIN_FINAL_LAG_MISMATCH",
    )
    committed(consumer, coords, high)


def run(source, root, handoff):
    """元のbrokerを読み、snapshot後にconsumer/カタログ/状態を再openする。"""
    from confluent_kafka import OFFSET_INVALID, Consumer, TopicPartition
    from consumer import message_record
    from contract_experiment import Experiment

    src.require(len(handoff) == 5, "CHAIN_HANDOFF_COUNT")
    coord = TopicPartition(src.TOPIC, 0)
    doc = {"version": 1, "tables": [TABLE]}
    receipt = []
    first_ids = None
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
                if start:
                    reopened_snapshots(catalog, first_ids, receipt)
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
                    src.require(
                        local_position(sqlite_url) == n,
                        "CHAIN_DURABLE_BEFORE_ACK",
                    )
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
                if end == 1:
                    first_ids = snapshots(catalog)
                    check_snapshot_rows(catalog, first_ids, receipt)
                if end == 5:
                    final_ids = snapshots(catalog)
                    src.require(
                        all(
                            final_ids[lane] != first_ids[lane]
                            for lane in LANES
                        ),
                        "CHAIN_FINAL_SNAPSHOT_UNCHANGED",
                    )
                    check_snapshot_rows(catalog, first_ids, receipt)
                    read_final_sql(source, catalog, final_ids)
                    verify_final_lag(
                        source, consumer, coord, sqlite_url, experiment
                    )
                    with closing(experiment.replay().db) as replayed:
                        count = replayed.execute(
                            "SELECT count(*) FROM accepted"
                        ).fetchone()
                    src.require(count == (5,), "CHAIN_REPLAY_COUNT")
        finally:
            consumer.close()
            catalog.engine.dispose()
