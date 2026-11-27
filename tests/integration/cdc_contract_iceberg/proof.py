"""合成一表だけの Kafka/Iceberg 境界を実データなしで検証する。"""

import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra/cdc"))

TOPIC = "contract_fixture.synthetic.example.orders"
TABLE = {
    "id": "orders",
    "enabled": True,
    "source": {
        "topic_prefix": "contract_fixture",
        "database": "synthetic",
        "schema": "example",
        "table": "orders",
    },
    "primary_key": ["factory_no", "order_code"],
    "mode": "current",
    "delete_policy": "remove",
    "snapshot": "initial",
    "destinations": {
        "raw": {"namespace": "proof", "table": "raw", "stream": "orders"},
        "current": {"namespace": "proof", "table": "current"},
        "silver": {"namespace": "proof", "table": "silver"},
    },
    "columns": [
        {"name": "factory_no", "type": "bigint", "nullable": False},
        {
            "name": "order_code",
            "type": "nvarchar",
            "width": 20,
            "nullable": False,
        },
        {
            "name": "price",
            "type": "decimal",
            "precision": 18,
            "scale": 3,
            "nullable": False,
        },
        {
            "name": "measured_at",
            "type": "datetime2",
            "scale": 7,
            "nullable": False,
        },
    ],
}
DOC = {"version": 1, "tables": [TABLE]}
FIRST = {
    "factory_no": 9_007_199_254_740_993,
    "order_code": "合成",
    "price": "123456789012345.120",
    "measured_at": 1_735_689_600_123_456_700,
}
SECOND = dict(FIRST, price="123456789012345.121")
LANES = ("raw", "current", "silver")
MOMENT = "2025-01-01T00:00:00Z"


class ProofFailure(ValueError):
    """運用情報を出さずに固定段階で失敗を返す。"""


def demand(condition, code):
    """assert を無効化しても受け入れ判定は残る。"""
    if not condition:
        raise ProofFailure(code)


def encode(value):
    """型付き成果物の表示でなく、入力同一性確認用の符号化を返す。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fixture_record(index):
    """初回像・更新・削除・tombstoneを実 codec で読む形にする。"""
    op, before, after = (
        ("r", None, FIRST),
        ("u", FIRST, SECOND),
        ("d", SECOND, None),
        (None, None, None),
    )[index]
    source = {
        "db": "synthetic",
        "schema": "example",
        "table": "orders",
        "commit_lsn": "00000001:00000001:0001",
        "change_lsn": f"00000001:00000001:{index + 1:04x}",
        "event_serial_no": index + 1,
    }
    return {
        "topic": TOPIC,
        "partition": 0,
        "offset": index,
        "key": encode(
            {name: FIRST[name] for name in reversed(TABLE["primary_key"])}
        ),
        "value": (
            None
            if op is None
            else encode(
                {"op": op, "before": before, "after": after, "source": source}
            )
        ),
        "ingested_at": MOMENT,
    }


def arrow_schema(lane):
    """正確な datetime2(7) は切り詰めず文字列、価格は decimal で持つ。"""
    import pyarrow as pa

    base = [pa.field("delivery_offset", pa.int64(), nullable=False)]
    if lane == "raw":
        return pa.schema(
            base
            + [
                pa.field("key_json", pa.string(), nullable=False),
                pa.field("value_json", pa.string()),
            ]
        )
    return pa.schema(
        base
        + [
            pa.field("factory_no", pa.int64()),
            pa.field("order_code", pa.string()),
            pa.field("price", pa.decimal128(18, 3)),
            pa.field("measured_at_exact", pa.string()),
        ]
    )


class Iceberg:
    """runner専用 SqlCatalog とローカル Parquet の実 commit 境界。"""

    def __init__(self, root, *, existing):
        from pyiceberg.catalog.sql import SqlCatalog

        self.silver_commit_rejected = False
        self.catalog = SqlCatalog(
            "contract-proof",
            uri=f"sqlite:///{root / 'catalog.db'}",
            warehouse=(root / "warehouse").as_uri(),
            **{"py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO"},
        )
        try:
            if not existing:
                self.catalog.create_namespace("proof")
                for lane in LANES:
                    self.catalog.create_table(
                        ("proof", lane), arrow_schema(lane)
                    )
            demand(
                set(self.catalog.list_tables("proof"))
                == {("proof", lane) for lane in LANES},
                "CATALOG_IDENTITY",
            )
        except Exception:
            self.close()
            raise

    def close(self):
        """カタログの SQLite ハンドルもプロセス再開境界で閉じる。"""
        self.catalog.engine.dispose()

    def read(self, lane, offset=None):
        """メタデータと Parquet をカタログから改めて読む。"""
        from pyiceberg.expressions import EqualTo

        table = self.catalog.load_table(("proof", lane))
        scan = (
            table.scan()
            if offset is None
            else table.scan(row_filter=EqualTo("delivery_offset", offset))
        )
        return scan.to_arrow().to_pylist()

    def ensure(self, lane, expected):
        """一配送一世代。再試行の不一致や重複は隠さない。"""
        import pyarrow as pa

        offset = expected["delivery_offset"]
        found = self.read(lane, offset)
        if not found:
            table = self.catalog.load_table(("proof", lane))
            try:
                table.append(
                    pa.Table.from_pylist([expected], schema=arrow_schema(lane))
                )
            except Exception as error:
                if (
                    lane == "silver"
                    and getattr(
                        getattr(error, "orig", None), "sqlite_errorcode", None
                    )
                    == sqlite3.SQLITE_CONSTRAINT_TRIGGER
                    and error.orig.args == ("FIXTURE_SILVER_COMMIT_REJECT",)
                ):
                    self.silver_commit_rejected = True
                raise
        demand(self.read(lane, offset) == [expected], "ICEBERG_EXACT_READBACK")


@contextmanager
def reopened(root, *, existing):
    """消えた state を空で作り直さず、同じ試行のファイルだけ再開する。"""
    from contract_experiment import Experiment

    catalog_path = root / "catalog.db"
    state_path = root / "state.db"
    if existing:
        demand(
            all(
                path.is_file() and not path.is_symlink()
                for path in (catalog_path, state_path)
            ),
            "LOCAL_STATE_MISSING",
        )
    else:
        demand(not list(root.iterdir()), "LOCAL_STATE_ALREADY_PRESENT")
    database = sqlite3.connect(
        state_path.as_uri() + ("?mode=rw" if existing else "?mode=rwc"),
        uri=True,
    )
    iceberg = None
    try:
        database.execute("PRAGMA synchronous=FULL")
        if existing:
            # Experiment also initializes a truly empty DB; resume must not.
            demand(
                database.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type='table' AND name='contract_experiment'"
                ).fetchone()
                == (1,),
                "LOCAL_STATE_SCHEMA_MISSING",
            )
        experiment = Experiment(DOC, database)
        iceberg = Iceberg(root, existing=existing)
        yield experiment, iceberg
    finally:
        if iceberg is not None:
            iceberg.close()
        database.close()


def projected(experiment, layer, offset):
    """候補投影を callback 内の単一行／削除状態から取得する。"""
    rows = experiment.rows(TABLE["id"], layer)
    demand(len(rows) <= 1, "DERIVED_ROW_COUNT")
    result = dict.fromkeys(
        ("factory_no", "order_code", "price", "measured_at_exact")
    )
    if rows:
        result = dict(rows[0])
        result["price"] = Decimal(result["price"])
        result["measured_at_exact"] = result.pop("measured_at")
    return {"delivery_offset": offset, **result}


def bridge(experiment, ice, record):
    """同一 consumer 入口で Iceberg 原本→派生を確定し、ACK を許可する。"""
    from consumer import process

    offset = record["offset"]
    demand(
        record["topic"] == TOPIC
        and record["partition"] == 0
        and offset == experiment.offset(TOPIC, 0) + 1,
        "UNEXPECTED_DELIVERY",
    )

    def apply(_admin, _state, events, _mapping, _topics, *, values):
        """本来の consumer hook から Raw と契約 materialize を通す。"""
        demand(len(events) == 1 and values is None, "UNEXPECTED_BATCH")
        event = events[0]
        ice.ensure(
            "raw",
            {
                "delivery_offset": offset,
                "key_json": event["key"],
                "value_json": event["value"],
            },
        )

        def materialize():
            """Iceberg両派生世代の実 readback 後だけ SQLite が確定する。"""
            ice.ensure("current", projected(experiment, "current", offset))
            ice.ensure("silver", projected(experiment, "silver", offset))

        return experiment.process(events, fail_materialization=materialize)

    process(None, None, [record], {}, {}, contract_hook=apply)
    demand(experiment.offset(TOPIC, 0) == offset, "LOCAL_OFFSET_NOT_DURABLE")
    return offset + 1


def verify(ice, offsets):
    """codecと独立した期待値で Raw、全世代、型落ち、二重適用を比較する。"""
    expected_raw = [
        {
            "delivery_offset": n,
            "key_json": fixture_record(n)["key"],
            "value_json": fixture_record(n)["value"],
        }
        for n in range(offsets[0])
    ]
    demand(
        sorted(ice.read("raw"), key=lambda row: row["delivery_offset"])
        == expected_raw,
        "RAW_RESULT",
    )
    for lane, count in zip(("current", "silver"), offsets[1:], strict=True):
        expected = [
            {
                "delivery_offset": n,
                "factory_no": FIRST["factory_no"] if n < 2 else None,
                "order_code": "合成" if n < 2 else None,
                "price": (
                    Decimal(f"123456789012345.12{n}") if n < 2 else None
                ),
                "measured_at_exact": (
                    "2025-01-01T00:00:00.1234567" if n < 2 else None
                ),
            }
            for n in range(count)
        ]
        demand(
            sorted(ice.read(lane), key=lambda row: row["delivery_offset"])
            == expected,
            "DERIVED_RESULT",
        )


def acknowledge(root, session, next_offset):
    """別の readonly 接続で durable offset を検証してから ACK する。"""
    with closing(
        sqlite3.connect((root / "state.db").as_uri() + "?mode=ro", uri=True)
    ) as reader:
        rows = reader.execute(
            "SELECT off FROM checkpoints WHERE topic=? AND part=0", (TOPIC,)
        ).fetchall()
    demand(rows == [(next_offset - 1,)], "LOCAL_CHECKPOINT_NOT_OBSERVED")
    session.ack(next_offset)


@contextmanager
def reject_silver_commit(root):
    """所有する試験カタログで Silver ポインタの実更新だけを拒否する。"""
    with closing(
        sqlite3.connect((root / "catalog.db").as_uri() + "?mode=rw", uri=True)
    ) as catalog:
        catalog.execute("""
            CREATE TRIGGER fixture_silver_failure
            BEFORE UPDATE OF metadata_location ON iceberg_tables
            WHEN OLD.catalog_name = 'contract-proof'
              AND OLD.table_namespace = 'proof' AND OLD.table_name = 'silver'
            BEGIN
              SELECT RAISE(ABORT, 'FIXTURE_SILVER_COMMIT_REJECT');
            END
        """)
        try:
            yield
        finally:
            catalog.execute("DROP TRIGGER fixture_silver_failure")


def require_resume_alignment(experiment, session):
    """位置の不一致を自動修復せず、配送やACKを始める前に拒否する。"""
    next_offset = experiment.offset(TOPIC, 0) + 1
    demand(session.committed() == next_offset, "BROKER_RESUME_POSITION")
    return next_offset


def scenario(root, transport):
    """失敗前後を別接続で読み、同じ broker 再配送だけを再開する。"""
    from contract_experiment import ContractBatchError

    with reopened(root, existing=False) as (experiment, ice):
        with transport.open() as session:
            record = session.receive(0)
            acknowledge(root, session, bridge(experiment, ice, record))
            record = session.receive(1)
            with reject_silver_commit(root):
                try:
                    bridge(experiment, ice, record)
                except ContractBatchError as failure:
                    demand(
                        str(failure) == "DERIVED_STORAGE_FAILED"
                        and ice.silver_commit_rejected,
                        "UNEXPECTED_FAILURE",
                    )
                else:
                    raise ProofFailure("INJECTED_FAILURE_NOT_OBSERVED")
            verify(ice, (2, 2, 1))
            demand(session.committed() == 1, "BROKER_ADVANCED_ON_FAILURE")
    with reopened(root, existing=True) as (experiment, ice):
        demand(experiment.offset(TOPIC, 0) == 0, "FAILED_LOCAL_OFFSET")
        with transport.open() as session:
            start = require_resume_alignment(experiment, session)
            for offset in range(start, 4):
                acknowledge(
                    root,
                    session,
                    bridge(experiment, ice, session.receive(offset)),
                )
            demand(session.committed() == 4, "BROKER_FINAL_POSITION")
        verify(ice, (4, 4, 4))
        replay = experiment.replay()
        replay.db.close()


class LocalTransport:
    """実Icebergだけを測る補助経路。Kafkaの結論には使わない。"""

    committed_offset = 0

    @contextmanager
    def open(self):
        """再開時は現在の補助 ACK 位置から配送し直す。"""
        self.next_offset = self.committed_offset
        yield self

    def receive(self, offset):
        """補助配送は固定の4配送だけを生成する。"""
        demand(self.next_offset == offset, "LOCAL_FIXTURE_POSITION")
        self.next_offset += 1
        return fixture_record(offset)

    def ack(self, offset):
        """進んだ配信を補助状態に記録する。"""
        demand(offset == self.next_offset, "LOCAL_FIXTURE_ACK")
        self.committed_offset = offset

    def committed(self):
        """補助側の位置。実ブローカーの証拠ではない。"""
        return self.committed_offset


def main():
    """外部入力/接続先なし。専用の一時領域以外を変更しない。"""
    from contract_experiment import validator

    validator().validate(DOC)
    os.umask(0o077)
    if sys.argv[1:] == ["--local-storage-only"]:
        transport = LocalTransport()
        result = "ICEBERG_STORAGE_ONLY (Kafka not exercised)"
    elif sys.argv[1:] == ["--hosted-kafka"]:
        from kafka_transport import KafkaTransport

        transport = KafkaTransport()
        transport.seed()
        result = "ISOLATED_KAFKA_ICEBERG"
    else:
        raise ProofFailure("EXPLICIT_FIXTURE_REQUIRED")
    with TemporaryDirectory(prefix="cdc-contract-proof-") as work:
        scenario(Path(work), transport)
    print("PASS:", result)


if __name__ == "__main__":
    try:
        main()
    except ProofFailure as error:
        print("FAIL:", error, file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("FAIL: FIXTURE_OPERATION", file=sys.stderr)
        sys.exit(1)
