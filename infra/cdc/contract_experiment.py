#!/usr/bin/env python3
"""明示的な合成実験だけで契約駆動batchと再開条件を検証する。"""

import importlib
import json
import os
import re
import sqlite3
import stat
import sys
from hashlib import sha256
from pathlib import Path

from event_limits import MAX_RECORD_BYTES
from fixed_source import ROOT

MAX_BATCH = 1_000
MAX_EVENTS = 100_000
MAX_RAW_BYTES = 64 * 1024 * 1024
FIELDS = {"topic", "partition", "offset", "key", "value", "ingested_at"}


class ContractBatchError(ValueError):
    """合成レシートに出せる固定の処理理由を保持する。"""


def encode(value):
    """ハッシュおよび記録に使う決定的なJSON表現を返す。"""
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def validator():
    """標準runtimeは変えず、選択時だけ契約CLIの検証関数を読む。"""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("cdc_contract")


def aliases(document):
    """enabled表だけを、既存SQL Serverの両topic形式に対応付ける。"""
    routes = {}
    for table in document["tables"]:
        if not table["enabled"]:
            continue
        source = table["source"]
        prefix, db = source["topic_prefix"], source["database"]
        name = source["schema"] + "." + source["table"]
        for topic in (prefix + "." + name, prefix + "." + db + "." + name):
            routes[topic] = table
    return routes


class FixedContractHook:
    """厳密に等しい固定6表だけを既存処理に橋渡しする。"""

    def __init__(self, document, legacy_process):
        expected = validator().read_document(
            ROOT / "contracts/cdc/fixed-demo-v1.json"
        )
        if validator().validate(document) != expected:
            raise ContractBatchError("FIXED_CONTRACT_NOT_EXACT")
        self.document = json.loads(encode(document))
        self.legacy_process = legacy_process

    def __call__(self, admin, state, records, mapping, topics, *, values):
        """宣言と実際のmapping/identityを照合後、通常の6表処理を呼ぶ。"""
        from config import topic_names
        from fixed_source import table_mapping

        if not isinstance(values, dict) or values.get("CDC_SOURCE_MODE") != (
            "fixed-demo"
        ):
            raise ContractBatchError("FIXED_CONTRACT_MODE_MISMATCH")
        projected = {}
        for table in self.document["tables"]:
            src = table["source"]
            if (src["database"], src["topic_prefix"]) != (
                values.get("CDC_SQLSERVER_DATABASE"),
                values.get("CDC_TOPIC_PREFIX"),
            ):
                raise ContractBatchError("FIXED_CONTRACT_SOURCE_MISMATCH")
            names = [
                col["name"]
                for col in table["columns"]
                if col["name"]
                not in {"run_id", "created_at", "original_values"}
            ]
            projected[table["id"]] = {
                "target_table": table["id"],
                "source_schema": src["schema"],
                "source_table": src["table"],
                "columns": {name: name for name in names},
            }
        if (
            projected != mapping
            or projected != table_mapping()
            or (topic_names(values, projected) != topics)
        ):
            raise ContractBatchError("FIXED_CONTRACT_ROUTES_MISMATCH")
        return self.legacy_process(
            admin, state, records, mapping, topics, values=values
        )


class Experiment:
    """合成用SQLiteでRawと、完了するまで不可分の派生処理を分ける。"""

    def __init__(self, document, connection):
        self.document = json.loads(encode(validator().validate(document)))
        self.routes = aliases(self.document)
        if not self.routes:
            raise ContractBatchError("NO_ENABLED_CONTRACT")
        self.db = connection
        self.identity = sha256(encode(self.document).encode()).hexdigest()
        existing = set(
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        )
        own_tables = {
            ("contract_experiment",),
            ("raw",),
            ("current",),
            ("accepted",),
            ("silver",),
            ("checkpoints",),
            ("quarantine",),
        }
        if existing:
            if existing != own_tables or connection.execute(
                "SELECT identity FROM contract_experiment"
            ).fetchall() != [(self.identity,)]:
                raise ContractBatchError("EXPERIMENT_IDENTITY_MISMATCH")
            return
        connection.executescript(
            """
            BEGIN;
            CREATE TABLE contract_experiment (identity TEXT NOT NULL);
            CREATE TABLE raw (
                topic TEXT, part INTEGER, off INTEGER, destination TEXT,
                key_json TEXT, envelope TEXT, record TEXT NOT NULL,
                PRIMARY KEY(topic,part,off)
            );
            CREATE TABLE accepted (
                seq INTEGER PRIMARY KEY,
                topic TEXT NOT NULL, part INTEGER NOT NULL,
                off INTEGER NOT NULL,
                UNIQUE(topic,part,off)
            );
            CREATE TABLE current (
                table_id TEXT, business_key TEXT, ordering TEXT,
                row TEXT, effect TEXT NOT NULL, deleted INTEGER NOT NULL,
                PRIMARY KEY(table_id,business_key)
            );
            CREATE TABLE silver (
                table_id TEXT, business_key TEXT, row TEXT,
                PRIMARY KEY(table_id,business_key)
            );
            CREATE TABLE checkpoints (
                topic TEXT, part INTEGER, off INTEGER,
                PRIMARY KEY(topic,part)
            );
            CREATE TABLE quarantine (
                topic TEXT, part INTEGER, off INTEGER, reason TEXT,
                PRIMARY KEY(topic,part,off)
            );
            """
        )
        with connection:
            connection.execute(
                "INSERT INTO contract_experiment VALUES (?)", (self.identity,)
            )

    def offset(self, topic, partition):
        """派生完了済みの位置だけを返す。"""
        row = self.db.execute(
            "SELECT off FROM checkpoints WHERE topic=? AND part=?",
            (topic, partition),
        ).fetchone()
        return row[0] if row else -1

    def raw_count(self):
        """保存されている配送識別子数を返す。"""
        return self.db.execute("SELECT count(*) FROM raw").fetchone()[0]

    def quarantine_count(self):
        """固定reason付きで止めた配送数を返す。"""
        return self.db.execute("SELECT count(*) FROM quarantine").fetchone()[0]

    def rows(self, table_id, layer="current"):
        """合成Current/Silverの業務像。公開レシートでは値を出さない。"""
        sql = {
            "current": "SELECT row FROM current WHERE table_id=? "
            "AND row IS NOT NULL ORDER BY business_key",
            "silver": "SELECT row FROM silver WHERE table_id=? "
            "AND row IS NOT NULL ORDER BY business_key",
        }[layer]
        return [
            json.loads(row[0]) for row in self.db.execute(sql, (table_id,))
        ]

    @staticmethod
    def delivery(record):
        """生の合成配送のshape・サイズを確認し、座標を返す。"""
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ContractBatchError("INVALID_TRANSPORT")
        topic, part, off = (
            record[key] for key in ("topic", "partition", "offset")
        )
        if (
            not isinstance(topic, str)
            or not re.fullmatch(r"[a-z0-9_.-]{1,249}", topic)
            or type(part) is not int
            or not 0 <= part <= 2**31 - 1
            or type(off) is not int
            or not 0 <= off < 2**63 - 1
            or not isinstance(record["ingested_at"], str)
            or not 1 <= len(record["ingested_at"]) <= 64
        ):
            raise ContractBatchError("INVALID_TRANSPORT")
        if any(
            value is not None and not isinstance(value, str)
            for value in (record["key"], record["value"])
        ):
            raise ContractBatchError("INVALID_TRANSPORT")
        try:
            size = sum(
                len(value.encode())
                for value in (
                    record["key"] or "",
                    record["value"] or "",
                    record["ingested_at"],
                )
            )
        except UnicodeError:
            raise ContractBatchError("INVALID_TRANSPORT") from None
        if size > MAX_RECORD_BYTES:
            raise ContractBatchError("RECORD_LIMIT_EXCEEDED")
        return topic, part, off

    def process(self, records, *, fail_materialization=None):
        """Raw後に型を検査し、全派生とlocal位置を一緒に確定する。"""
        from contract_values import decode

        if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH:
            raise ContractBatchError("BATCH_LIMIT_EXCEEDED")
        deliveries = [(self.delivery(record), record) for record in records]
        count = self.raw_count()
        used = self.db.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(record AS BLOB))),0) FROM raw"
        ).fetchone()[0]
        with self.db:
            for coords, record in deliveries:
                table = self.routes.get(coords[0])
                dest = table["destinations"]["raw"] if table else None
                values = (record["key"], record["value"])
                existing = self.db.execute(
                    "SELECT key_json,envelope FROM raw "
                    "WHERE topic=? AND part=? AND off=?",
                    coords,
                ).fetchone()
                if existing is not None:
                    if existing != values:
                        raise ContractBatchError("DELIVERY_PAYLOAD_CHANGED")
                    continue
                count += 1
                if count > MAX_EVENTS:
                    raise ContractBatchError("RAW_LIMIT_EXCEEDED")
                raw = encode(record)
                used += len(raw.encode())
                if used > MAX_RAW_BYTES:
                    raise ContractBatchError("RAW_SIZE_LIMIT_EXCEEDED")
                self.db.execute(
                    "INSERT INTO raw VALUES (?,?,?,?,?,?,?)",
                    (*coords, encode(dest), *values, raw),
                )

        try:
            with self.db:
                for coords, record in deliveries:
                    topic, partition, offset = coords
                    completed = self.offset(topic, partition)
                    if offset <= completed:
                        continue
                    table = self.routes.get(topic)
                    if table is None:
                        raise ContractBatchError("UNREGISTERED_TOPIC")
                    if offset != completed + 1:
                        raise ContractBatchError("OFFSET_GAP")
                    try:
                        decoded = decode(table, record)
                    except ValueError:
                        raise ContractBatchError(
                            "INVALID_CONTRACT_EVENT"
                        ) from None
                    self.project(table, coords, decoded)
                    self.db.execute(
                        "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)",
                        coords,
                    )
                    # Raw may include failed or shuffled attempts. Only this
                    # transaction establishes the actual accepted order.
                    self.db.execute(
                        "INSERT INTO accepted (topic,part,off) VALUES (?,?,?)",
                        coords,
                    )
                if fail_materialization is not None:
                    try:
                        fail_materialization()
                    except Exception:
                        raise ContractBatchError(
                            "DERIVED_STORAGE_FAILED"
                        ) from None
        except ContractBatchError as error:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO quarantine VALUES (?,?,?,?)",
                    (*coords, str(error)),
                )
            raise
        return {"raw": self.raw_count(), "quarantine": self.quarantine_count()}

    def project(self, table, coords, decoded):
        """順序・削除版を保ち、単純な型付きSilver像に展開する。"""
        operation, key, row, order = decoded
        if operation == "tombstone":
            return
        table_id = table["id"]
        if table["mode"] == "append":
            key = encode(coords)
            row = {"op": operation, "key": json.loads(decoded[1]), "row": row}
        effect = sha256(encode((operation, key, row)).encode()).hexdigest()
        previous = self.db.execute(
            "SELECT ordering,row,effect,deleted FROM current "
            "WHERE table_id=? AND business_key=?",
            (table_id, key),
        ).fetchone()
        if previous is not None:
            old_order = json.loads(previous[0])
            if old_order == order and previous[2] != effect:
                raise ContractBatchError("SOURCE_VERSION_PAYLOAD_CHANGED")
            if old_order >= order:
                return
        if operation == "d" and table["delete_policy"] == "retain":
            latest = (
                json.loads(previous[1]) if previous and previous[1] else None
            )
            if latest is not None and previous[3] == 1:
                latest = latest["row"]
            row = {"deleted": True, "row": latest}
        payload = encode(row) if row is not None else None
        self.db.execute(
            "INSERT OR REPLACE INTO current VALUES (?,?,?,?,?,?)",
            (
                table_id,
                key,
                encode(order),
                payload,
                effect,
                int(operation == "d"),
            ),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO silver VALUES (?,?,?)",
            (table_id, key, payload),
        )

    def snapshot(self):
        """内部の派生行・版・確定位置を比較用に固定する。"""
        queries = (
            "SELECT * FROM current ORDER BY 1,2",
            "SELECT * FROM silver ORDER BY 1,2",
            "SELECT * FROM checkpoints ORDER BY 1,2",
            "SELECT topic,part,off FROM accepted ORDER BY seq",
        )
        return tuple(self.db.execute(query).fetchall() for query in queries)

    def replay(self):
        """同じ受理順で別の合成stateを作り、派生状態・位置も照合する。"""
        candidate = Experiment(self.document, sqlite3.connect(":memory:"))
        try:
            records = self.db.execute(
                "SELECT raw.record FROM accepted LEFT JOIN raw ON "
                "raw.topic=accepted.topic AND raw.part=accepted.part "
                "AND raw.off=accepted.off ORDER BY accepted.seq"
            )
            for (record,) in records:
                if record is None:
                    raise ContractBatchError("REPLAY_RAW_MISSING")
                candidate.process([json.loads(record)])
            if self.snapshot() != candidate.snapshot():
                raise ContractBatchError("REPLAY_STATE_MISMATCH")
            return candidate
        except Exception:
            candidate.db.close()
            raise


def preview(contract_path, records_path):
    """ネットワークなしで指定された合成入力を同一batch境界で試す。"""
    from consumer import process

    loader = validator()
    document = loader.validate(loader.read_document(Path(contract_path)))
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        with os.fdopen(os.open(records_path, flags), "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ContractBatchError("INVALID_SYNTHETIC_FILE")
            data = source.read(MAX_RECORD_BYTES + 1)
    except OSError:
        raise ContractBatchError("INVALID_SYNTHETIC_FILE") from None
    if len(data) > MAX_RECORD_BYTES:
        raise ContractBatchError("INPUT_LIMIT_EXCEEDED")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractBatchError("INVALID_SYNTHETIC_JSON")
            result[key] = value
        return result

    records = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    db = sqlite3.connect(":memory:")
    try:
        experiment = Experiment(document, db)
        return process(
            None,
            None,
            records,
            {},
            {},
            contract_hook=lambda _a, _s, events, _m, _t, *, values: (
                experiment.process(events)
            ),
        )
    finally:
        db.close()
