"""固定CDC契約を本番経路と照合する。実DB・Kafka・Trinoには接続しない。"""

import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/cdc"))
import config  # noqa: E402
import connector  # noqa: E402
import consumer  # noqa: E402
import fixed_source  # noqa: E402
import raw_writer  # noqa: E402
from state import State  # noqa: E402

TABLES = json.loads(
    (ROOT / "contracts/cdc/fixed-demo-v1.json").read_text(encoding="utf-8")
)["tables"]
RUN_ID = "012345abcdef"
BUSINESS_TIME = "2025-01-01T00:00:00.7654321Z"
SERVER_NANOS = 1_735_689_600_123_456_700
SERVER_TIME = "2025-01-01T00:00:00.1234567Z"


def ddl_definition(ddl, schema, table):
    """固定SQLのCREATE TABLEから、列順・型・NULL性と主キーを読む。"""
    qualified = re.escape(f"[{schema}].[{table}]")
    match = re.search(
        r"CREATE TABLE " + qualified + r" \(\n(.*?)\n\);", ddl, re.S
    )
    if match is None:
        raise AssertionError("fixed SQL table definition missing")
    body = match[1]
    key = re.search(r"PRIMARY KEY \(\[(\w+)\]\)", body)[1]
    columns = []
    for line in body.splitlines():
        if not line.lstrip().startswith("["):
            continue
        match = re.match(
            r"\s*\[(\w+)\] (\w+)(?:\(([^)]+)\))? (NOT NULL|NULL)", line
        )
        if match is None:
            raise AssertionError("unsupported fixed SQL column definition")
        name, kind, args, null = match.groups()
        spec = {"name": name, "type": kind, "nullable": null == "NULL"}
        if kind in {"char", "nvarchar"}:
            spec["width"] = "max" if args == "max" else int(args)
        elif kind == "decimal":
            spec["precision"], spec["scale"] = map(int, args.split(","))
        elif kind == "datetime2":
            spec["scale"] = int(args)
        elif kind not in {"bigint", "date"} or args is not None:
            raise AssertionError("unexpected fixed SQL column type")
        columns.append(spec)
    return columns, [key]


def fixture_settings(database):
    """本番の連携設定を、接続しない合成値だけで構成する。"""
    return fixed_source.select_settings(
        {
            "CDC_SOURCE_MODE": "fixed-demo",
            "SOURCE_SQLSERVER_HOST": "sql-source.example.invalid",
            "SOURCE_SQLSERVER_DATABASE": database,
            "SOURCE_SQLSERVER_USER": "fixture_reader",
            "SOURCE_SQLSERVER_PASSWORD": "synthetic-reader-value",
            "DEMO_SQLSERVER_INSTALLATION_ID": "a" * 32,
            "DEMO_SQLSERVER_OWNERSHIP_TOKEN": "b" * 64,
        }
    )


def images(columns, pk, key):
    """型付き列・原値・SQL時刻を意図的に区別する合成行を作る。"""
    original = dict.fromkeys(columns)
    original[pk] = key
    original[columns[1]] = ""
    original["updated_at"] = BUSINESS_TIME
    after = {
        pk: key,
        columns[1]: None,
        "updated_at": SERVER_NANOS,
        "run_id": RUN_ID,
        "original_values": json.dumps(original),
    }
    return original, after


def delivery(topic, source, pk, key, offset, op, image):
    """裸JSONの単一キーと Debezium の配送形式を組み立てる。"""
    envelope = None
    if op is not None:
        envelope = json.dumps(
            {
                "op": op,
                "source": {
                    "db": source["database"],
                    "schema": source["schema"],
                    "table": source["table"],
                    "commit_lsn": "00000001:00000001:0001",
                    "change_lsn": "00000002:00000002:0002",
                    "event_serial_no": offset,
                },
                "before": {pk: key} if op in {"u", "d"} else None,
                "after": image if op != "d" else None,
            }
        )
    return {
        "topic": topic,
        "partition": 0,
        "offset": offset,
        "key": json.dumps({pk: key}),
        "value": envelope,
        "ingested_at": "2025-01-01T00:00:01Z",
    }


class FixedContractParityTests(unittest.TestCase):
    """fixtureの記述と既存の固定sourceの所有権・データ契約を照合する。"""

    def test_source_fixture_matches_contract_ddl_and_capture(self):
        """manifest・adapter・DDLのいずれかがずれたら検知する。"""
        adapter = fixed_source.source_adapter()
        mapping = fixed_source.table_mapping()
        ddl = (ROOT / "infra/sqlserver/002_demo_tables.sql").read_text()
        captures = (
            ROOT / "infra/sqlserver/004_demo_cdc_enable.sql"
        ).read_text()
        self.assertEqual(len(TABLES), 6)
        self.assertEqual(
            [fixture["id"] for fixture in TABLES], list(adapter.TABLES)
        )
        self.assertEqual(list(mapping), list(adapter.TABLES))
        for fixture in TABLES:
            name = fixture["id"]
            source = fixture["source"]
            schema, table = source["schema"], source["table"]
            with self.subTest(table=name):
                self.assertIs(fixture["enabled"], True)
                columns, key = ddl_definition(ddl, schema, table)
                self.assertEqual(fixture["columns"], columns)
                self.assertEqual(fixture["primary_key"], key)
                business = [
                    column["name"]
                    for column in columns
                    if column["name"] not in fixed_source.EXTRA_COLUMNS
                ]
                self.assertEqual(
                    adapter.TABLES[name],
                    {
                        "schema": schema,
                        "table": table,
                        "columns": business,
                        "pk": key[0],
                    },
                )
                self.assertEqual(
                    mapping[name],
                    {
                        "target_table": name,
                        "source_schema": schema,
                        "source_table": table,
                        "columns": dict(zip(business, business, strict=True)),
                    },
                )
                capture = f"{schema}_{table}_v1"
                self.assertEqual(adapter.CAPTURE_INSTANCES[name], capture)
                self.assertIn(f"N'{capture}'", captures)

    def test_topics_connector_and_silver_destinations(self):
        """実際の命名関数・登録設定と、銀層SQLへの入力を照合する。"""
        mapping = fixed_source.table_mapping()
        values = fixture_settings("fixture_db")
        settings = connector.configuration(values, mapping)
        topics = config.topic_names(values, mapping)
        silver = (ROOT / "medallion/01_silver.sql").read_text()
        table_filters = []
        column_filters = []
        expected_topics = {}
        for fixture in TABLES:
            name = fixture["id"]
            source = fixture["source"]
            origin = f"{source['schema']}.{source['table']}"
            self.assertEqual(source["topic_prefix"], settings["topic.prefix"])
            self.assertEqual(
                source["database"], values["CDC_SQLSERVER_DATABASE"]
            )
            prefix = source["topic_prefix"]
            expected_topics[f"{prefix}.{origin}"] = name
            expected_topics[f"{prefix}.{source['database']}.{origin}"] = name
            table_filters.append(re.escape(origin))
            column_filters.extend(
                re.escape(origin + "." + column["name"])
                for column in fixture["columns"]
            )
            self.assertEqual(fixture["snapshot"], settings["snapshot.mode"])
            self.assertEqual(
                fixture["destinations"]["silver"],
                {
                    "namespace": "sqlcdc_silver",
                    "table": name,
                },
            )
            self.assertIn(
                f"CREATE OR REPLACE TABLE iceberg.silver.{name} AS", silver
            )
            self.assertIn(f"FROM iceberg.bronze.{name}", silver)
        self.assertEqual(expected_topics, topics)
        self.assertCountEqual(
            table_filters, settings["table.include.list"].split(",")
        )
        self.assertCountEqual(
            column_filters, settings["column.include.list"].split(",")
        )
        for key, value in {
            "snapshot.isolation.mode": "exclusive",
            "decimal.handling.mode": "string",
            "time.precision.mode": "adaptive",
            "tombstones.on.delete": "true",
            "key.converter.schemas.enable": "false",
            "value.converter.schemas.enable": "false",
            "topic.creation.default.partitions": "1",
            "database.user": "${env:SOURCE_SQLSERVER_USER}",
            "database.password": "${env:SOURCE_SQLSERVER_PASSWORD}",
        }.items():
            self.assertEqual(settings[key], value)
        self.assertIn("FROM_ISO8601_TIMESTAMP(updated_at)", silver)

    def test_raw_and_current_lifecycle(self):
        """全6表の合成c/u/d/tombstoneを、本番分岐とSQLiteで照合する。"""
        mapping = fixed_source.table_mapping()
        values = fixture_settings("fixture_db")
        identity = consumer.source_identity(values, mapping)
        topics = config.topic_names(values, mapping)
        store = State(":memory:", identity)
        self.addCleanup(store.connection.close)
        admin = Mock()
        materialized = Mock(return_value={"sql_statements": 14})
        for fixture in TABLES:
            name = fixture["id"]
            with self.subTest(table=name):
                self.assert_lifecycle(
                    fixture,
                    mapping,
                    values,
                    topics,
                    store,
                    admin,
                    materialized,
                )

    def assert_lifecycle(
        self, fixture, mapping, values, topics, store, admin, materialized
    ):
        """観測境界をmockし、原値・更新順序・削除を実処理へ渡す。"""
        name = fixture["id"]
        source = fixture["source"]
        pk = fixture["primary_key"][0]
        columns = list(mapping[name]["columns"])
        key = "fixture-" + name
        provided, after = images(columns, pk, key)
        topic = ".".join(
            source[field]
            for field in ("topic_prefix", "database", "schema", "table")
        )
        results = []

        def keep_raw(catalog, records, *, namespace, identity):
            """保存先は実経路で検出し、Raw投影は本番変換を呼ぶ。"""
            raw = fixture["destinations"]["raw"]
            self.assertIs(catalog, admin.catalog.return_value)
            self.assertEqual(namespace, raw["namespace"])
            self.assertEqual(raw["table"], "events")
            self.assertEqual(len(identity), 64)
            results.extend(raw_writer.raw_rows(records, fixed=True))
            return len(records)

        def apply(offset, op, image):
            """materializeの外部依存だけを止め、offsetの完了まで実行する。"""
            record = delivery(topic, source, pk, key, offset, op, image)
            with (
                patch.object(consumer, "append_raw", side_effect=keep_raw),
                patch.object(consumer, "materialize", materialized),
            ):
                consumer.process(
                    admin, store, [record], mapping, topics, values=values
                )
            return record

        self.assertEqual(fixture["mode"], "current")
        self.assertEqual(fixture["delete_policy"], "remove")
        current = fixture["destinations"]["current"]
        self.assertEqual(current["table"], name)
        snapshot = apply(0, "r", after)
        self.assertEqual(results[-1]["operation"], "r")
        self.assertEqual(store.rows(name)[0]["_source_pk"], key)
        before = apply(1, "c", after)
        row = store.rows(name)[0]
        self.assertEqual({c: row[c] for c in columns}, provided)
        self.assertEqual(row["_source_updated_at"], SERVER_TIME)
        self.assertEqual(row["_source_pk"], key)
        self.assertEqual(row["_run_id"], RUN_ID)
        self.assertEqual(results[-1]["envelope_json"], before["value"])
        self.assertEqual(json.loads(results[-1]["event_id"]), [topic, 0, 1])
        updated = {**provided, columns[1]: "合成"}
        apply(2, "u", {**after, "original_values": json.dumps(updated)})
        self.assertEqual(store.rows(name)[0][columns[1]], "合成")
        apply(3, "d", None)
        self.assertEqual(store.rows(name), [])
        self.assertEqual(results[-1]["operation"], "d")
        tombstone = apply(4, None, None)
        self.assertEqual(results[-1]["operation"], "tombstone")
        self.assertEqual(results[-1]["tombstone"], "true")
        self.assertIsNone(results[-1]["envelope_json"])
        empty = raw_writer.raw_rows([{**tombstone, "value": ""}], fixed=True)[
            0
        ]
        self.assertEqual(empty["envelope_json"], "")
        self.assertEqual(empty["tombstone"], "false")
        # DELETE後に到着した古い版が現在値を復活させない。
        for stale in (snapshot, before):
            store.stage(stale, name, mapping[name], columns)
        self.assertEqual(store.rows(name), [])
        self.assertEqual(store.offset(topic, 0), 4)
        materialized.assert_called_with(
            admin, store, prefix=current["namespace"].removesuffix("bronze")
        )
