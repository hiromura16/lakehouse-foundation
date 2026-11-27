"""合成5表の元イベントを実Iceberg/SQLiteで検証するローカルfixture。

SQL、Kafka、運用catalogへ接続しない。別表同士のIceberg commitは
atomicではなく、試験専用の部分世代が見え得る。完了位置のゲートだけ
が、全ての必要な行のreadbackより先に進まないことを確認する。
"""

import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import fixture

MAX_BATCH = 96
PLAN = {spec.topic: tuple(fixture.steps(spec)) for spec in fixture.TABLES}
SPECS = {spec.topic: spec for spec in fixture.TABLES}
CATALOG = "contract-scale-proof"
NS = "fixture_scale"
FAULT = "FIXTURE_SCALE_SILVER_REJECT"


def schema(lane):
    """小数はArrow decimal、datetime2(7)は桁を失わない文字列。"""
    import pyarrow as pa

    def required(name, kind):
        return pa.field(name, kind, nullable=False)

    if lane == "raw":
        return pa.schema(
            [
                required("topic", pa.string()),
                required("partition", pa.int32()),
                required("offset", pa.int64()),
                required("key_json", pa.string()),
                pa.field("value_json", pa.string()),
            ]
        )
    return pa.schema(
        [
            required("site_id", pa.string()),
            required("record_id", pa.int32()),
            pa.field("label", pa.string()),
            required("quantity", pa.decimal128(18, 3)),
            required("observed_at_exact", pa.string()),
        ]
    )


def typed(rows):
    """契約decoderで正規化した値を実Arrow保存値へ変換する。"""
    return [
        {
            "site_id": row["site_id"],
            "record_id": row["record_id"],
            "label": row["label"],
            "quantity": Decimal(row["quantity"]),
            "observed_at_exact": row["observed_at"],
        }
        for row in sorted(rows, key=lambda row: row["record_id"])
    ]


def expected_row_types(spec):
    """fixtureの独立した期待式から、保存先が持つべき値を作る。"""
    # Keep this independent of typed(), the conversion used on writes.
    return [
        {
            "site_id": row["site_id"],
            "record_id": row["record_id"],
            "label": row["label"],
            "quantity": Decimal(row["quantity"]),
            "observed_at_exact": row["observed_at"],
        }
        for row in fixture.final_rows(spec)
    ]


def raw_row(record):
    """mtime等と無関係な配送座標、key、wire payloadを保存する。"""
    return {
        "topic": record["topic"],
        "partition": record["partition"],
        "offset": record["offset"],
        "key_json": record["key"],
        "value_json": record["value"],
    }


def same_wire(observed, deliveries):
    """書き込み用変換を使わず、元の配送と保存した全座標・byte列を比較。"""
    return [
        (
            row["topic"],
            row["partition"],
            row["offset"],
            row["key_json"],
            row["value_json"],
        )
        for row in observed
    ] == [
        (
            original["topic"],
            original["partition"],
            original["offset"],
            original["key"],
            original["value"],
        )
        for original in deliveries
    ]


class Harness:
    """固定名前空間・状態を、新しい所有一時領域の中だけに作る。"""

    def __init__(self):
        self.workspace = TemporaryDirectory(prefix="cdc-scale-storage-")
        self.root = Path(self.workspace.name)
        self.store = None
        self.db = None
        self.experiment = None
        self.observed_fault = False
        self.contract = fixture.contract()
        self.definitions = {row["id"]: row for row in self.contract["tables"]}
        try:
            self.open(existing=False)
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        self.close()

    def disconnect(self):
        """再開前に既存接続を閉じ、同じfixtureのみを読み直す。"""
        if self.store is not None:
            self.store.engine.dispose()
            self.store = None
        if self.db is not None:
            self.db.close()
            self.db = None
        self.experiment = None

    def close(self):
        """所有する試験領域だけを破棄する。"""
        self.disconnect()
        self.workspace.cleanup()

    def open(self, *, existing):
        """実SQLite SqlCatalogを固定のローカル倉庫で初期化する。"""
        from contract_experiment import Experiment
        from pyiceberg.catalog.sql import SqlCatalog

        fixture.demand(
            self.db is None and self.store is None,
            "SCALE_DUPLICATE_CONNECTION",
        )
        states = self.root / "state.db"
        catalog = self.root / "catalog.db"
        warehouse = self.root / "warehouse"
        if existing:
            fixture.demand(
                all(
                    path.exists() and not path.is_symlink()
                    for path in (states, catalog, warehouse)
                ),
                "SCALE_OWNED_STATE_MISSING",
            )
        else:
            fixture.demand(not tuple(self.root.iterdir()), "SCALE_NOT_EMPTY")
        self.db = sqlite3.connect(
            states.as_uri() + ("?mode=rw" if existing else "?mode=rwc"),
            uri=True,
        )
        self.db.execute("PRAGMA synchronous=FULL")
        if existing:
            fixture.demand(
                self.db.execute(
                    "SELECT name FROM sqlite_schema "
                    "WHERE name='contract_experiment'"
                ).fetchall()
                == [("contract_experiment",)],
                "SCALE_OWNED_STATE_SCHEMA",
            )
        self.experiment = Experiment(self.contract, self.db)
        if existing:
            with closing(
                sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
            ) as reader:
                fixture.demand(
                    reader.execute(
                        "SELECT iceberg_type FROM iceberg_tables LIMIT 1"
                    ).fetchall()
                    == [("TABLE",)],
                    "SCALE_OWNED_CATALOG_SCHEMA",
                )
        self.store = SqlCatalog(
            CATALOG,
            uri=f"sqlite:///{catalog}",
            warehouse=warehouse.as_uri(),
            init_catalog_tables="false" if existing else "true",
            **{"py-io-impl": "pyiceberg.io.pyarrow.PyArrowFileIO"},
        )
        expected = {
            self.target(name, lane)
            for name in self.definitions
            for lane in ("raw", *fixture.LANES)
        }
        if not existing:
            self.store.create_namespace(NS)
            for name in self.definitions:
                for lane in ("raw", *fixture.LANES):
                    self.store.create_table(
                        self.target(name, lane), schema(lane)
                    )
        fixture.demand(
            set(self.store.list_tables(NS)) == expected,
            "SCALE_CATALOG_IDENTITY",
        )

    def reopen(self):
        """同一プロセス内で接続境界を開き直す。hard crashとは別。"""
        self.disconnect()
        self.open(existing=True)

    def target(self, name, lane):
        """fixtureの固定契約以外から表名を受け取らない。"""
        ref = self.definitions[name]["destinations"][lane]
        fixture.demand(ref["namespace"] == NS, "SCALE_NAMESPACE_MISMATCH")
        return (NS, ref["table"])

    def read(self, name, lane):
        """実catalogから現在snapshotを読み、配送/業務キーで整列する。"""
        table = self.store.load_table(self.target(name, lane))
        key = "offset" if lane == "raw" else "record_id"
        table_rows = table.scan(limit=513).to_arrow()
        fixture.demand(table_rows.num_rows <= 512, "SCALE_TABLE_LIMIT")
        return sorted(table_rows.to_pylist(), key=lambda row: row[key])

    def append_raw(self, spec, records):
        """対象座標の再送は同一wireの場合だけ冪等。"""
        import pyarrow as pa

        existing_rows = self.read(spec.name, "raw")
        existing = {row["offset"]: row for row in existing_rows}
        fixture.demand(
            len(existing) == len(existing_rows), "SCALE_RAW_DUPLICATE"
        )
        wanted = [raw_row(record) for record in records]
        pending = []
        for item in wanted:
            offset = item["offset"]
            if offset in existing:
                fixture.demand(existing[offset] == item, "SCALE_RAW_CONFLICT")
            else:
                pending.append(item)
        if pending:
            self.store.load_table(self.target(spec.name, "raw")).append(
                pa.Table.from_pylist(pending, schema=schema("raw"))
            )
        written = self.read(spec.name, "raw")
        expected = sorted(
            existing_rows + pending, key=lambda row: row["offset"]
        )
        fixture.demand(written == expected, "SCALE_RAW_READBACK_MISMATCH")

    def materialize(self, names):
        """全対象表の確定readback。途中失敗ではSQLite側を確定しない。"""
        import pyarrow as pa
        from sqlalchemy.exc import IntegrityError

        for name in sorted(names):
            limit = (
                next(
                    spec.initial
                    for spec in fixture.TABLES
                    if spec.name == name
                )
                + MAX_BATCH
            )
            for lane in fixture.LANES:
                rows = self.experiment.rows(name, lane)
                fixture.demand(len(rows) <= limit, "SCALE_DERIVED_LIMIT")
                wanted = typed(rows)
                previous = self.read(name, lane)
                if previous != wanted:
                    try:
                        table = self.store.load_table(self.target(name, lane))
                        write = table.overwrite if previous else table.append
                        write(
                            pa.Table.from_pylist(wanted, schema=schema(lane))
                        )
                    except IntegrityError as error:
                        if (
                            getattr(error.orig, "sqlite_errorcode", None)
                            == sqlite3.SQLITE_CONSTRAINT_TRIGGER
                            and error.orig.args == (FAULT,)
                        ):
                            self.observed_fault = True
                        raise
                fixture.demand(
                    self.read(name, lane) == wanted,
                    "SCALE_DERIVED_READBACK_MISMATCH",
                )

    def offsets(self):
        """既存の別read-only SQLite接続から確定位置だけを確認する。"""
        path = (self.root / "state.db").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(path, uri=True)) as reader:
            actual = reader.execute(
                "SELECT topic,part,off FROM checkpoints LIMIT 6"
            ).fetchall()
        fixture.demand(
            len(actual) <= len(SPECS)
            and all(
                isinstance(topic, str)
                and topic in SPECS
                and type(part) is int
                and part == 0
                and type(offset) is int
                and 0 <= offset < len(PLAN[topic])
                for topic, part, offset in actual
            ),
            "SCALE_CHECKPOINT_SCOPE",
        )
        offsets = {topic: offset for topic, _, offset in actual}
        fixture.demand(len(offsets) == len(actual), "SCALE_CHECKPOINT_SCOPE")
        return offsets

    def write(self, records):
        """実Consumerのhook入口から、公開可能な件数だけを返す。"""
        from consumer import process

        fixture.demand(
            isinstance(records, list) and 1 <= len(records) <= MAX_BATCH,
            "SCALE_BATCH_LIMIT",
        )
        expected = self.offsets()
        by_topic = {}
        for record in records:
            topic, _, _ = self.experiment.delivery(record)
            fixture.demand(topic in SPECS, "SCALE_UNKNOWN_TOPIC")
            idx = expected.get(topic, -1) + 1
            fixture.demand(idx < len(PLAN[topic]), "SCALE_EXTRA_DELIVERY")
            fixture.verify_record(record, SPECS[topic], idx, PLAN[topic][idx])
            expected[topic] = idx
            by_topic.setdefault(topic, []).append(record)
        changed = {SPECS[topic].name for topic in by_topic}
        before = self.offsets()

        def hook(_admin, _state, data, _maps, _topics, *, values):
            fixture.demand(
                values is None and data is records, "SCALE_UNEXPECTED_HOOK"
            )
            for topic, bucket in sorted(by_topic.items()):
                self.append_raw(SPECS[topic], bucket)

            def reviewed_readback():
                self.materialize(changed)
                fixture.demand(
                    self.offsets() == before,
                    "SCALE_LOCAL_ADVANCED_BEFORE_STORAGE",
                )

            return self.experiment.process(
                data, fail_materialization=reviewed_readback
            )

        process(None, None, records, {}, {}, contract_hook=hook)
        fixture.demand(
            self.offsets() == expected, "SCALE_LOCAL_OFFSETS_NOT_DURABLE"
        )
        return {"accepted": len(records), "affected_tables": len(changed)}

    def verify_final(self, original):
        """完全wire配列と、独立計算による最終業務像を再照合する。"""
        counts = fixture.counts()
        total = 0
        for spec in fixture.TABLES:
            raw = self.read(spec.name, "raw")
            expected = [row for row in original if row["topic"] == spec.topic]
            fixture.demand(
                same_wire(raw, expected) and len(raw) == len(PLAN[spec.topic]),
                "SCALE_FINAL_RAW_MISMATCH",
            )
            total += len(raw)
            fixture.demand(
                self.offsets().get(spec.topic) == len(PLAN[spec.topic]) - 1,
                "SCALE_FINAL_OFFSET_MISMATCH",
            )
            for lane in fixture.LANES:
                fixture.demand(
                    self.read(spec.name, lane) == expected_row_types(spec),
                    "SCALE_FINAL_TYPED_ROWS_MISMATCH",
                )
        fixture.demand(
            total
            == counts["snapshot"]
            + counts["insert"]
            + counts["update"]
            + counts["delete"]
            + counts["tombstone"],
            "SCALE_FINAL_COUNT_MISMATCH",
        )
        return {
            **counts,
            "raw_rows": total,
            "current_rows": counts["snapshot"],
            "silver_rows": counts["snapshot"],
        }
