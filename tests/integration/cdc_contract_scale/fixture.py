"""5表の合成CDC件数・業務行の照合。実ネットワークは使わない。"""

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PREFIX = "fixture_scale"
DATABASE = "fixture_scale_sql"
SCHEMA = "dbo"
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "infra/cdc"))
LANES = ("current", "silver")
START = datetime(2026, 1, 2, tzinfo=UTC)
INGESTED = "2026-01-02T00:00:00Z"
SITE = "synthetic"
KEY = ("site_id", "record_id")


@dataclass(frozen=True, slots=True)
class Table:
    """表間で共通の型と、表別に固定する件数・名前。"""

    name: str
    initial: int
    cycles: int

    @property
    def topic(self):
        """一表一topicに分け、global offsetを想定しない。"""
        return f"{PREFIX}.{DATABASE}.{SCHEMA}.{self.name}"


TABLES = (
    Table("fixture_alpha", 16, 32),
    Table("fixture_bravo", 20, 40),
    Table("fixture_charlie", 24, 48),
    Table("fixture_delta", 24, 64),
    Table("fixture_echo", 28, 72),
)


def demand(condition, code):
    """入力本文を公開ログに渡さず、固定理由で失敗する。"""
    if not condition:
        raise ValueError(code)


def contract():
    """実験用契約。ただし運用既定や既存6表には登録しない。"""
    columns = [
        dict(name="site_id", type="nvarchar", width=20, nullable=False),
        dict(name="record_id", type="int", nullable=False),
        dict(name="label", type="nvarchar", width=40, nullable=True),
        dict(
            name="quantity",
            type="decimal",
            precision=18,
            scale=3,
            nullable=False,
        ),
        dict(name="observed_at", type="datetime2", scale=7, nullable=False),
    ]
    tables = []
    for spec in TABLES:
        tables.append(
            {
                "id": spec.name,
                "enabled": True,
                "source": {
                    "topic_prefix": PREFIX,
                    "database": DATABASE,
                    "schema": SCHEMA,
                    "table": spec.name,
                },
                "primary_key": list(KEY),
                "mode": "current",
                "delete_policy": "remove",
                "snapshot": "initial",
                "destinations": {
                    "raw": {
                        "namespace": "fixture_scale",
                        "table": f"raw_{spec.name}",
                        "stream": spec.name,
                    },
                    "current": {
                        "namespace": "fixture_scale",
                        "table": f"cur_{spec.name}",
                    },
                    "silver": {
                        "namespace": "fixture_scale",
                        "table": f"silver_{spec.name}",
                    },
                },
                "columns": [dict(col) for col in columns],
            }
        )
    return {"version": 1, "tables": tables}


def make_row(spec, record_id, generation, *, changed=False):
    """NULL/空文字、正負decimal、7桁時刻を含む正規の合成行。"""
    table_marker = TABLES.index(spec) * 10_000_000
    amount = (
        123_456_789_000_000_000 + table_marker + record_id * 1_000 + generation
    )
    quantity = Decimal((-1 if changed else 1) * amount).scaleb(-3)
    label = (None, "", "合成")[(record_id + generation) % 3]
    base_seconds = int(START.timestamp())
    nanos = (base_seconds + record_id) * 1_000_000_000
    nanos += (generation + 1) * 1_234_500
    return {
        "site_id": SITE,
        "record_id": record_id,
        "label": label,
        "quantity": str(quantity),
        "observed_at": nanos,
    }


@dataclass(frozen=True, slots=True)
class Step:
    """SQL発行計画と、各topicで本来現れる変更順を別々に持つ。"""

    op: str
    key: int
    before: dict | None
    after: dict | None


def steps(spec):
    """snapshot後、1周期I/U/Dと既存行Uの4変更（tombstone別）を生む。"""
    initial = {i: make_row(spec, i, 0) for i in range(1, spec.initial + 1)}
    for key, row in initial.items():
        yield Step("r", key, None, row)
    for i in range(spec.cycles):
        generation = i + 1
        temporary_key = 1_000 + generation
        incoming = make_row(spec, temporary_key, generation)
        changed = make_row(spec, temporary_key, generation, changed=True)
        yield Step("c", temporary_key, None, incoming)
        yield Step("u", temporary_key, incoming, changed)
        yield Step("d", temporary_key, changed, None)
        yield Step("tombstone", temporary_key, None, None)
        stable_key = i % spec.initial + 1
        current = make_row(spec, stable_key, generation, changed=True)
        yield Step("u", stable_key, initial[stable_key], current)
        initial[stable_key] = current


def table_identifier(spec):
    """出所の固定5表以外のSQL識別子を組み立てない。"""
    demand(spec in TABLES, "UNKNOWN_SYNTHETIC_TABLE")
    return f"[dbo].[{spec.name}]"


def snapshot_query(spec):
    """SQL Serverで行順を偶然の実行計画に任せないための専用query。"""
    ident = table_identifier(spec)
    return (
        "SELECT site_id, record_id, label, quantity, observed_at FROM "
        + ident
        + " ORDER BY site_id, record_id"
    )


def sql_stamp(epoch_nanos):
    """SQLのdatetime2(7)に渡す桁を整数のまま維持する。"""
    seconds, nanos = divmod(epoch_nanos, 1_000_000_000)
    demand(nanos % 100 == 0, "SQL_TIMESTAMP_PRECISION")
    when = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{when}.{nanos // 100:07d}"


def sql_step(spec, step):
    """実fixtureで後に発行する、固定のDMLと別束縛の引数だけを返す。"""
    ident = table_identifier(spec)
    if step.op == "tombstone":
        # 別のSQLは行わない。Debeziumのdelete時に付随するKafka null。
        return None
    where = " WHERE site_id=%s AND record_id=%s"
    index = (SITE, step.key)
    if step.op == "d":
        return "DELETE FROM " + ident + where, index
    demand(step.op in {"r", "c", "u"}, "UNKNOWN_SYNTHETIC_OPERATION")
    row = step.after
    args = (row["label"], row["quantity"], sql_stamp(row["observed_at"]))
    if step.op == "u":
        return (
            "UPDATE " + ident + " SET label=%s, "
            "quantity=CONVERT(decimal(18,3),%s), "
            "observed_at=CONVERT(datetime2(7),%s)" + where,
            args + index,
        )
    return (
        "INSERT INTO " + ident + " "
        "(site_id,record_id,label,quantity,observed_at) "
        "VALUES (%s,%s,%s,CONVERT(decimal(18,3),%s),CONVERT(datetime2(7),%s))",
        index + args,
    )


def counts():
    """source I/U/D、snapshot、Kafka nullを別の数として表に出す。"""
    return {
        "tables": len(TABLES),
        "snapshot": sum(t.initial for t in TABLES),
        "insert": sum(t.cycles for t in TABLES),
        "update": sum(t.cycles * 2 for t in TABLES),
        "delete": sum(t.cycles for t in TABLES),
        "tombstone": sum(t.cycles for t in TABLES),
    }


def encode(value):
    """合成配送のバイト表現を毎回同じにする。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def modeled(spec, offset, step):
    """本物のConnectではない、offline照合専用の裸JSON配送。"""
    lsn = f"00000001:{offset + 1:08x}:0001"
    body = {
        "before": step.before,
        "after": step.after,
        "op": step.op,
        "source": {
            "db": DATABASE,
            "schema": SCHEMA,
            "table": spec.name,
            "snapshot": "true" if step.op == "r" else "false",
            "change_lsn": lsn,
            "commit_lsn": lsn,
            "event_serial_no": 1,
        },
    }
    return {
        "topic": spec.topic,
        "partition": 0,
        "offset": offset,
        "key": encode(dict(site_id=SITE, record_id=step.key)),
        "value": None if step.op == "tombstone" else encode(body),
        "ingested_at": INGESTED,
    }


def deliveries():
    """別表間の配送順に依存しない照合を試すため、topicを交互に選ぶ。"""
    plans = [(spec, enumerate(steps(spec))) for spec in TABLES]
    while plans:
        active = []
        for spec, plan in plans:
            delivery = next(plan, None)
            if delivery is not None:
                offset, step = delivery
                yield modeled(spec, offset, step)
                active.append((spec, plan))
        plans = active


def final_rows(spec):
    """変更列や行の生存をイベントreplayから独立した式で求める。"""
    rows = []
    for key in range(1, spec.initial + 1):
        # 次の同じ行の更新は initial 回後。最後の更新番号を逆算する。
        n = spec.cycles - 1
        generation = (n - (n - (key - 1)) % spec.initial) + 1
        demand(generation >= 1, "INVALID_SYNTHETIC_DISTRIBUTION")
        amount = Decimal(
            123456789000000000
            + TABLES.index(spec) * 10000000
            + key * 1000
            + generation
        ) / Decimal(-1000)
        base = (START + timedelta(seconds=key)).strftime("%Y-%m-%dT%H:%M:%S")
        fraction = ((generation + 1) * 1234500) // 100
        rows.append(
            {
                "site_id": SITE,
                "record_id": key,
                "label": [None, "", "合成"][(key + generation) % 3],
                "quantity": f"{amount:.3f}",
                "observed_at": f"{base}.{fraction:07d}",
            }
        )
    return rows


def verify_record(record, spec, index, step):
    """別topicの流入、型違い、画像差、余分な重複を中断する。"""
    from contract_values import parse

    demand(
        type(record.get("partition")) is int
        and record["partition"] == 0
        and type(record.get("offset")) is int
        and record["offset"] == index,
        "SCALE_TOPIC_POSITION",
    )
    key = parse(record["key"])
    demand(
        isinstance(key, dict)
        and tuple(key) == KEY
        and key["site_id"] == SITE
        and type(key["record_id"]) is int
        and key["record_id"] == step.key,
        "SCALE_KEY_MISMATCH",
    )
    if step.op == "tombstone":
        demand(record["value"] is None, "SCALE_EXPECTED_KAFKA_NULL")
        return
    observed = parse(record["value"])
    demand(
        isinstance(observed, dict)
        and observed.get("op") == step.op
        and "before" in observed
        and "after" in observed
        and observed["before"] == step.before
        and observed["after"] == step.after,
        "SCALE_ROW_OR_ORDER",
    )
    origin = observed.get("source")
    demand(
        isinstance(origin, dict)
        and (origin.get("db"), origin.get("schema"), origin.get("table"))
        == (DATABASE, SCHEMA, spec.name),
        "SCALE_SOURCE_MISMATCH",
    )


def reconcile(records):
    """実験Consumer hookとSQLiteを通り、原本件数・独立した最終像を照合。"""
    from consumer import process
    from contract_experiment import Experiment

    specs = {item.topic: item for item in TABLES}
    plans = {spec.topic: iter(steps(spec)) for spec in TABLES}
    positions = defaultdict(int)
    counted = Counter()
    with closing(sqlite3.connect(":memory:")) as state:
        adapter = Experiment(contract(), state)

        def apply(_admin, _state, events, _mapping, _topics, *, values):
            demand(values is None, "UNEXPECTED_SCALE_RUNTIME")
            return adapter.process(events)

        for record in records:
            topic = record.get("topic")
            demand(topic in specs, "SCALE_UNKNOWN_TOPIC")
            stage = next(plans[topic], None)
            demand(stage is not None, "SCALE_EXTRA_DELIVERY")
            verify_record(record, specs[topic], positions[topic], stage)
            process(None, None, [record], {}, {}, contract_hook=apply)
            positions[topic] += 1
            counted[stage.op] += 1
        demand(
            all(next(plan, None) is None for plan in plans.values()),
            "SCALE_MISSING_DELIVERY",
        )
        expected = counts()
        demand(
            counted
            == {
                "r": expected["snapshot"],
                "c": expected["insert"],
                "u": expected["update"],
                "d": expected["delete"],
                "tombstone": expected["tombstone"],
            }
            and adapter.raw_count() == sum(counted.values())
            and adapter.quarantine_count() == 0,
            "SCALE_EXACT_COUNTS",
        )
        for spec in TABLES:
            demand(
                adapter.offset(spec.topic, 0)
                == spec.initial + 5 * spec.cycles - 1,
                "SCALE_FINAL_OFFSET",
            )
            for lane in LANES:
                actual = sorted(
                    adapter.rows(spec.name, lane),
                    key=lambda row: row["record_id"],
                )
                demand(actual == final_rows(spec), "SCALE_FINAL_ROWS")
        with closing(adapter.replay().db) as rebuilt:
            count = rebuilt.execute("SELECT count(*) FROM accepted").fetchone()
        demand(count == (sum(counted.values()),), "SCALE_REPLAY_COUNT")
        return expected


if __name__ == "__main__":
    try:
        result = reconcile(deliveries())
    except Exception:
        print("SCALE_MODEL=fail", file=sys.stderr)
        raise SystemExit(1) from None
    print(
        "SCALE_MODEL=pass " + " ".join(f"{k}={v}" for k, v in result.items())
    )
