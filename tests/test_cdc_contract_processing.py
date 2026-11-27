"""CDCを実処理の入口で検証する。SQL・Kafka・分析サービスへは接続しない。"""

import contextlib
import io
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from cdc_fixtures import (
    MAPPING,
    VALUES,
    consumer,
    pa,
    record,
)
from config import topic_names
from state import State

ROOT = Path(__file__).resolve().parents[1]
FIXED_DOCUMENT = ROOT / "contracts/cdc/fixed-demo-v1.json"
FIXED_VALUES = {
    **VALUES,
    "SOURCE_SQLSERVER_DATABASE": "fixture_db",
    "CDC_SQLSERVER_DATABASE": "fixture_db",
}
FIXED_TOPICS = topic_names(FIXED_VALUES, MAPPING)
FIXED_IDENTITY = consumer.source_identity(FIXED_VALUES, MAPPING)


class MemoryTable:
    """Arrowの型と配送IDによる選択を保つテスト用の保存境界。"""

    def __init__(self, schema, properties=None):
        """作成条件と空の行セットを保持する。"""
        self._schema = schema
        self.properties = properties or {}
        self.rows = []

    def schema(self):
        """保存側が使うArrowの型を返す。"""
        return SimpleNamespace(as_arrow=lambda: self._schema)

    def append(self, arrow):
        """実処理で作ったArrow行を追記する。"""
        self.rows.extend(arrow.to_pylist())

    def overwrite(self, arrow):
        """実処理で作ったCurrentの全行を置き換える。"""
        self.rows = arrow.to_pylist()

    def scan(self, *, row_filter, selected_fields):
        """配送IDで絞り、指定された列だけを既存行から読み取る。"""
        field, accepted = row_filter
        schema = pa.schema(
            [self._schema.field(name) for name in selected_fields]
        )
        rows = [
            {name: row[name] for name in selected_fields}
            for row in self.rows
            if row[field] in accepted
        ]
        return SimpleNamespace(
            to_arrow=lambda: pa.Table.from_pylist(rows, schema=schema)
        )


class MemoryCatalog:
    """ネットワークを使わず、作成・検索先と行を記録する。"""

    def __init__(self):
        """独立した一試行の保存領域を用意する。"""
        self.namespaces = set()
        self.tables = {}

    def namespace_exists(self, name):
        """namespaceの存在を返す。"""
        return name in self.namespaces

    def create_namespace(self, name):
        """指定のnamespaceを登録する。"""
        self.namespaces.add(name)

    def table_exists(self, name):
        """表の存在を返す。"""
        return name in self.tables

    def create_table(self, name, *, schema, properties=None):
        """指定の型・identityを持つ空の表を登録する。"""
        if name in self.tables:
            raise AssertionError("unexpected table recreation")
        self.tables[name] = MemoryTable(schema, properties)

    def load_table(self, name):
        """存在しない宛先を黙認せず、登録済みの表だけを返す。"""
        return self.tables[name]


class MemoryAdmin:
    """実consumerの入出力を観測し、Trino実行だけを置き換える。"""

    def __init__(self):
        """試行ごとにcatalog・完了記録を独立させる。"""
        self._catalog = MemoryCatalog()
        self.STATE = {"blocked": False}
        self.medallion_calls = []
        self.fail_analysis = False

    def catalog(self):
        """実処理から使われる単一catalogを返す。"""
        return self._catalog

    def ensure_tables(self, prefix):
        """固定Bronzeの業務列と追跡列を持つ空表を用意する。"""
        namespace = prefix + "bronze"
        self._catalog.create_namespace(namespace)
        metadata = (
            "_source_system",
            "_source_database",
            "_source_schema",
            "_source_table",
            "_source_pk",
            "_source_updated_at",
            "_batch_id",
            "_run_id",
            "_source_file",
            "_source_hash",
            "_ingested_at",
        )
        for name, mapping in MAPPING.items():
            target = namespace + "." + name
            if not self._catalog.table_exists(target):
                columns = list(mapping["columns"]) + list(metadata)
                self._catalog.create_table(
                    target,
                    schema=pa.schema(
                        [(name, pa.string()) for name in columns]
                    ),
                )

    def medallion(self, prefix):
        """SQL実行境界に届いたprefixを記録し、成功か失敗を返す。"""
        self.medallion_calls.append(prefix)
        if self.fail_analysis:
            raise RuntimeError("synthetic analysis failure")
        return {"sql_statements": 14}


@contextlib.contextmanager
def expressions_boundary():
    """選択式だけを置換し、外部プリミティブのない環境でも動かす。"""
    package = ModuleType("pyiceberg")
    package.__path__ = []
    expressions = ModuleType("pyiceberg.expressions")
    expressions.In = lambda field, values: (field, frozenset(values))
    replacements = {"pyiceberg": package, "pyiceberg.expressions": expressions}
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        # pyarrow first imports pandas lazily and caches its classes. Do not
        # remove unrelated modules loaded while using this fake.
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def fixed_batches():
    """6表それぞれのr/c/u/d/tombstoneと、同一配送の再送を作る。"""
    snapshots = []
    inserts = []
    updates = []
    deletes = []
    tombstones = []
    for table in MAPPING:
        snapshots.append(record(table, 0, "r"))
        inserts.append(record(table, 1, "c"))
        updates.append(record(table, 2, "u", sequence=2, name="合成改訂"))
        deletes.append(record(table, 3, "d", sequence=3))
        tombstones.append({**deletes[-1], "offset": 4, "value": None})

    def bind(batch):
        """既存合成行を、契約が指定する検証専用DB名へ揃える。"""
        result = []
        for row in batch:
            payload = row["value"]
            if payload is not None:
                envelope = json.loads(payload)
                envelope["source"]["db"] = "fixture_db"
                payload = json.dumps(envelope, ensure_ascii=False)
            topic = row["topic"].replace(".demo_database.", ".fixture_db.", 1)
            result.append({**row, "topic": topic, "value": payload})
        return result

    return tuple(
        map(
            bind,
            (
                snapshots,
                inserts,
                updates,
                deletes,
                tombstones,
                tombstones,
            ),
        )
    )


def observe_fixed(run, cleanup):
    """保存行・offsetを各確定点で採取し、試行間で比較できる形にする。"""
    admin = MemoryAdmin()
    state = State(":memory:", FIXED_IDENTITY)
    cleanup(state.connection.close)
    observations = []
    with expressions_boundary():
        for batch in fixed_batches():
            result = run(admin, state, batch)
            tables = admin.catalog().tables
            observations.append(
                {
                    "result": result,
                    "offset": state.completed_offsets(),
                    "raw": list(tables["sqlcdc_events.events"].rows),
                    "current": {
                        table: list(tables["sqlcdc_bronze." + table].rows)
                        for table in MAPPING
                    },
                }
            )
    return observations, admin.medallion_calls


def legacy_run(admin, state, batch):
    """フラグを指定しない既存の固定処理を実行する。"""
    return consumer.process(
        admin, state, batch, MAPPING, FIXED_TOPICS, values=FIXED_VALUES
    )


class ContractProcessingTests(unittest.TestCase):
    """固定CDCの互換性と障害時の完了境界を確認する。"""

    def test_expression_fake_preserves_other_lazy_imports(self):
        """Fake Icebergの解除時に、別moduleの登録を消さない。"""
        name = "synthetic_lazy_module_during_pyiceberg_fake"
        module = ModuleType(name)
        try:
            with expressions_boundary():
                sys.modules[name] = module
            self.assertIs(sys.modules.get(name), module)
        finally:
            sys.modules.pop(name, None)

    def test_legacy_persists_raw_current_and_completed_offsets(self):
        """フラグなしで既存の保存先と配送・削除規約が変わらない。"""
        stages, calls = observe_fixed(legacy_run, self.addCleanup)
        self.assertEqual(calls, ["sqlcdc_"] * 6)
        self.assertEqual(
            [len(stage["raw"]) for stage in stages], [6, 12, 18, 24, 30, 30]
        )
        self.assertEqual(
            [stage["result"]["raw_events_appended"] for stage in stages],
            [6, 6, 6, 6, 6, 0],
        )
        for stage in stages[:3]:
            self.assertTrue(
                all(len(rows) == 1 for rows in stage["current"].values())
            )
        for stage in stages[3:]:
            self.assertTrue(
                all(not rows for rows in stage["current"].values())
            )
        row = stages[2]["current"]["erp_products"][0]
        self.assertEqual(row["product_name"], "合成改訂")
        self.assertEqual(row["standard_cycle_seconds"], "123456789.123")
        self.assertEqual(
            row["_source_updated_at"], "2026-04-01T12:00:00.1234567Z"
        )
        self.assertIsNone(row["product_family"])
        self.assertEqual(row["unit"], "")
        tombstones = [row for row in stages[-1]["raw"] if row["offset"] == "4"]
        self.assertEqual(len(tombstones), 6)
        self.assertTrue(
            all(row["envelope_json"] is None for row in tombstones)
        )
        self.assertEqual(len(stages[-1]["offset"]), 6)
        self.assertTrue(
            all(offset == 4 for offset in stages[-1]["offset"].values())
        )

    def test_opt_in_fixed_matches_legacy_at_every_stage(self):
        """固定contract経路のRaw、Current、offsetを既定経路と比較する。"""
        from contract_experiment import FixedContractHook

        contract = json.loads(FIXED_DOCUMENT.read_text(encoding="utf-8"))
        hook = FixedContractHook(contract, consumer.process)

        def contracted(admin, state, batch):
            """実consumerの明示hook入口を通る。"""
            return consumer.process(
                admin,
                state,
                batch,
                MAPPING,
                FIXED_TOPICS,
                values=FIXED_VALUES,
                contract_hook=hook,
            )

        expected = observe_fixed(legacy_run, self.addCleanup)
        actual = observe_fixed(contracted, self.addCleanup)
        self.assertEqual(actual, expected)

    def test_opt_in_refuses_route_drift_before_any_writes(self):
        """固定のtopic割当が欠ければ、既存処理に渡す前に拒否する。"""
        from contract_experiment import ContractBatchError, FixedContractHook

        contract = json.loads(FIXED_DOCUMENT.read_text(encoding="utf-8"))
        hook = FixedContractHook(contract, consumer.process)
        admin = MemoryAdmin()
        state = State(":memory:", FIXED_IDENTITY)
        self.addCleanup(state.connection.close)
        routes = dict(FIXED_TOPICS)
        routes.pop(next(iter(routes)))
        with self.assertRaisesRegex(
            ContractBatchError, "FIXED_CONTRACT_ROUTES_MISMATCH"
        ):
            consumer.process(
                admin,
                state,
                fixed_batches()[0],
                MAPPING,
                routes,
                values=FIXED_VALUES,
                contract_hook=hook,
            )
        self.assertEqual(state.completed_offsets(), {})
        self.assertEqual(admin.catalog().tables, {})

    def test_fixed_analysis_failure_keeps_raw_but_not_completed_offsets(self):
        """派生の失敗ではRawに証拠を残し、完了位置を一切進めない。"""
        from contract_experiment import FixedContractHook

        contract = json.loads(FIXED_DOCUMENT.read_text(encoding="utf-8"))
        hook = FixedContractHook(contract, consumer.process)
        admin = MemoryAdmin()
        admin.fail_analysis = True
        state = State(":memory:", FIXED_IDENTITY)
        self.addCleanup(state.connection.close)
        batch = fixed_batches()[0]
        with (
            expressions_boundary(),
            self.assertRaisesRegex(RuntimeError, "synthetic analysis failure"),
        ):
            consumer.process(
                admin,
                state,
                batch,
                MAPPING,
                FIXED_TOPICS,
                values=FIXED_VALUES,
                contract_hook=hook,
            )
        self.assertEqual(state.completed_offsets(), {})
        self.assertEqual(state.get("materialization_pending"), "true")
        self.assertEqual(
            len(admin.catalog().tables["sqlcdc_events.events"].rows), 6
        )


DYNAMIC_TABLE = {
    "id": "test_orders",
    "enabled": True,
    "source": {
        "topic_prefix": "fixture",
        "database": "fixture_db",
        "schema": "example",
        "table": "orders",
    },
    "primary_key": ["factory_no", "order_code"],
    "mode": "current",
    "delete_policy": "remove",
    "snapshot": "initial",
    "destinations": {
        "raw": {
            "namespace": "fixture_raw",
            "table": "events",
            "stream": "test",
        },
        "current": {"namespace": "fixture_current", "table": "orders"},
        "silver": {"namespace": "fixture_silver", "table": "orders"},
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
        {"name": "comment", "type": "nvarchar", "width": 48, "nullable": True},
    ],
}
DYNAMIC_TOPIC = "fixture.fixture_db.example.orders"
DYNAMIC_ROW = {
    "factory_no": 9_007_199_254_740_993,
    "order_code": "検証",
    "price": "123456789012345.12",
    "measured_at": 1_735_689_600_123_456_700,
    "comment": None,
}


def typed_delivery(offset, op="c", *, before=None, after=None):
    """複合キーは逆順のJSONにし、契約の順序と独立であることを確認する。"""
    key = {name: DYNAMIC_ROW[name] for name in ("order_code", "factory_no")}
    origin = {
        "db": "fixture_db",
        "schema": "example",
        "table": "orders",
        "commit_lsn": "00000001:00000001:0001",
        "change_lsn": f"00000001:00000001:{offset:04x}",
        "event_serial_no": offset,
    }
    payload = {
        "op": op,
        "source": origin,
        "before": before,
        "after": after,
    }
    return {
        "topic": DYNAMIC_TOPIC,
        "partition": 0,
        "offset": offset,
        "key": json.dumps(key, ensure_ascii=False),
        "value": None
        if op is None
        else json.dumps(payload, ensure_ascii=False),
        "ingested_at": "2025-01-01T00:00:01Z",
    }


class DynamicContractProcessingTests(unittest.TestCase):
    """登録済みの合成契約をローカルSQLiteに反映する境界だけを試験する。"""

    def make_experiment(self, *, table=None, path=":memory:"):
        """独立した接続を用意し、実サービスから切り離す。"""
        from contract_experiment import Experiment

        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        return Experiment(
            {
                "version": 1,
                "tables": [DYNAMIC_TABLE if table is None else table],
            },
            connection,
        )

    def test_typed_composite_key_current_raw_and_replay(self):
        """複合キーの改訂、NULL/空文字、精度と削除を再生できる。"""
        experiment = self.make_experiment()
        first = typed_delivery(0, after=DYNAMIC_ROW)
        experiment.process([first])
        current = experiment.rows("test_orders")[0]
        self.assertEqual(current["factory_no"], DYNAMIC_ROW["factory_no"])
        self.assertEqual(current["order_code"], "検証")
        self.assertEqual(current["price"], "123456789012345.120")
        self.assertEqual(current["measured_at"], "2025-01-01T00:00:00.1234567")
        self.assertIsNone(current["comment"])
        updated = {**DYNAMIC_ROW, "comment": ""}
        change = typed_delivery(1, "u", before=DYNAMIC_ROW, after=updated)
        experiment.process([change])
        self.assertEqual(len(experiment.rows("test_orders")), 1)
        self.assertEqual(experiment.rows("test_orders")[0]["comment"], "")
        self.assertEqual(
            experiment.rows("test_orders", layer="silver")[0]["price"],
            current["price"],
        )
        experiment.process([change])
        self.assertEqual(experiment.raw_count(), 2)
        expected = experiment.rows("test_orders")
        replayed = experiment.replay()
        self.addCleanup(replayed.db.close)
        self.assertIsNot(replayed, experiment)
        self.assertEqual(replayed.rows("test_orders"), expected)
        self.assertEqual(experiment.offset(DYNAMIC_TOPIC, 0), 1)
        delete = typed_delivery(2, "d", before=updated)
        experiment.process([delete, typed_delivery(3, None)])
        self.assertEqual(experiment.rows("test_orders"), [])
        self.assertEqual(experiment.raw_count(), 4)
        self.assertEqual(experiment.offset(DYNAMIC_TOPIC, 0), 3)

    def test_wrong_topic_bad_image_and_storage_fault_do_not_complete(self):
        """不明配送・型不一致・派生失敗で完了境界を前進させない。"""
        from contract_experiment import ContractBatchError

        valid = typed_delivery(0, after=DYNAMIC_ROW)
        invalid = typed_delivery(0, after={**DYNAMIC_ROW, "price": 19})
        for bad in ({**valid, "topic": "fixture.unknown.orders"}, invalid):
            with self.subTest(topic=bad["topic"]):
                experiment = self.make_experiment()
                with self.assertRaises(ContractBatchError):
                    experiment.process([bad])
                self.assertEqual(experiment.raw_count(), 1)
                self.assertEqual(experiment.quarantine_count(), 1)
                self.assertEqual(experiment.offset(bad["topic"], 0), -1)
                self.assertEqual(experiment.rows("test_orders"), [])

        experiment = self.make_experiment()

        def fail(*_args):
            """派生をこの試験だけ確定させない。"""
            raise OSError("synthetic private failure")

        with self.assertRaises(ContractBatchError) as failure:
            experiment.process([valid], fail_materialization=fail)
        self.assertNotIn("synthetic private failure", str(failure.exception))
        self.assertEqual(experiment.raw_count(), 1)
        self.assertEqual(experiment.offset(DYNAMIC_TOPIC, 0), -1)
        self.assertEqual(experiment.rows("test_orders"), [])
        self.assertEqual(experiment.rows("test_orders", layer="silver"), [])

    def test_preview_cli_requires_explicit_complete_opt_in(self):
        """無効・引数不足は運用runを呼ばず、処理開始前に停止する。"""
        for options, env in (
            (["--contract-preview", "unused.json"], {}),
            (
                ["--records-file", "unused.json"],
                {"CDC_CONTRACT_EXPERIMENT": "true"},
            ),
            ([], {"CDC_CONTRACT_EXPERIMENT": "true"}),
        ):
            with self.subTest(options=options, env=env):
                output = io.StringIO()
                self.addCleanup(logging.disable, logging.root.manager.disable)
                with (
                    patch.object(consumer, "run") as run,
                    patch.object(sys, "argv", ["consumer.py", *options]),
                    patch.dict(os.environ, env, clear=True),
                    contextlib.redirect_stdout(output),
                    self.assertRaises(SystemExit) as stopped,
                ):
                    consumer.main()
                run.assert_not_called()
                self.assertEqual(stopped.exception.code, 1)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"failed": True, "error_type": "ValueError"},
                )

    def test_preview_cli_processes_synthetic_only(self):
        """明示フラグと合成ファイルだけで既存のrunを起動せず実処理する。"""
        with tempfile.TemporaryDirectory() as directory:
            contract = Path(directory) / "contract.json"
            records = Path(directory) / "records.json"
            contract.write_text(
                json.dumps({"version": 1, "tables": [DYNAMIC_TABLE]}),
                encoding="utf-8",
            )
            records.write_text(
                json.dumps([typed_delivery(0, after=DYNAMIC_ROW)]),
                encoding="utf-8",
            )
            options = [
                "consumer.py",
                "--contract-preview",
                str(contract),
                "--records-file",
                str(records),
            ]
            output = io.StringIO()
            self.addCleanup(logging.disable, logging.root.manager.disable)
            with (
                patch.object(consumer, "run") as run,
                patch.object(sys, "argv", options),
                patch.dict(
                    os.environ, {"CDC_CONTRACT_EXPERIMENT": "true"}, clear=True
                ),
                contextlib.redirect_stdout(output),
            ):
                consumer.main()
            run.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertIs(receipt["synthetic_only"], True)
        self.assertEqual(receipt["raw"], 1)
        self.assertEqual(receipt["quarantine"], 0)
        self.assertNotIn("fixture_db", output.getvalue())

    def test_runtime_acks_only_after_completed_sqlite(self):
        """既存Runtimeから実consumer hookを通し、ACKの順序を確認する。"""
        from cdc_fixtures import partition
        from consumer_runtime import Runtime

        valid = typed_delivery(0, after=DYNAMIC_ROW)
        invalid = typed_delivery(0, after={**DYNAMIC_ROW, "price": 19})

        def fail():
            """合成の派生処理だけを失敗させる。"""
            raise OSError("synthetic private failure")

        for candidate, fault, success in (
            (valid, None, True),
            (invalid, None, False),
            (valid, fail, False),
        ):
            with self.subTest(valid=success, storage_fault=fault is not None):
                directory = self.enterContext(tempfile.TemporaryDirectory())
                path = Path(directory) / "fresh-preview.db"
                experiment = self.make_experiment(path=path)
                calls = []

                def reopened_offset():
                    """別connectionから確定済みcheckpointだけを読む。"""
                    with contextlib.closing(sqlite3.connect(path)) as observer:
                        row = observer.execute(
                            "SELECT off FROM checkpoints "
                            "WHERE topic=? AND part=?",
                            (DYNAMIC_TOPIC, 0),
                        ).fetchone()
                    return row[0] if row else -1

                state = State(":memory:", FIXED_IDENTITY)
                self.addCleanup(state.connection.close)

                def hook(_admin, _state, records, _map, _topics, *, values):
                    """試行のレコードを実験用SQLiteへ渡す。"""
                    self.assertIs(values, FIXED_VALUES)
                    calls.append(("process", reopened_offset()))
                    return experiment.process(
                        records, fail_materialization=fault
                    )

                def process_batch(*args, **kwargs):
                    """既存フック入口を迂回せず、同じconsumerを呼ぶ。"""
                    return consumer.process(
                        *args, **kwargs, contract_hook=hook
                    )

                def acknowledge(*, offsets, asynchronous):
                    """同期ACK時点でSQLite側が確定済みかを観測する。"""
                    self.assertFalse(asynchronous)
                    self.assertFalse(experiment.db.in_transaction)
                    self.assertEqual(
                        [
                            (rec.topic, rec.partition, rec.offset)
                            for rec in offsets
                        ],
                        [(DYNAMIC_TOPIC, 0, 1)],
                    )
                    calls.append(("ack", reopened_offset()))

                broker = Mock()
                broker.commit.side_effect = acknowledge
                with patch.object(Runtime, "observe"):
                    runtime = Runtime(
                        MemoryAdmin(),
                        state,
                        FIXED_VALUES,
                        MAPPING,
                        FIXED_TOPICS,
                        Mock(),
                        broker,
                        partition,
                        process_batch,
                        Mock(),
                    )
                    runtime.partitions = [partition(DYNAMIC_TOPIC, 0)]
                    result = runtime.process([candidate])
                self.assertEqual(experiment.raw_count(), 1)
                if success:
                    self.assertEqual(calls, [("process", -1), ("ack", 0)])
                    self.assertEqual(result["raw"], 1)
                    broker.commit.assert_called_once()
                    broker.pause.assert_not_called()
                else:
                    self.assertIsNone(result)
                    self.assertEqual(calls, [("process", -1)])
                    self.assertEqual(experiment.offset(DYNAMIC_TOPIC, 0), -1)
                    self.assertEqual(reopened_offset(), -1)
                    self.assertEqual(experiment.rows("test_orders"), [])
                    broker.commit.assert_not_called()
                    broker.pause.assert_called_once_with(runtime.partitions)
                    self.assertEqual(
                        runtime.error_code, "CDC_PROCESSING_FAILED"
                    )

    def test_preview_real_process_receipts(self):
        """実CLIに最小環境だけ渡し、終了値・秘匿済み受領票を検証する。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = root / "contract.json"
            records = root / "records.json"
            guarded = root / "forbidden-state"
            contract.write_text(
                json.dumps({"version": 1, "tables": [DYNAMIC_TABLE]}),
                encoding="utf-8",
            )
            records.write_text(
                json.dumps([typed_delivery(0, after=DYNAMIC_ROW)]),
                encoding="utf-8",
            )
            arguments = [
                "--contract-preview",
                str(contract),
                "--records-file",
                str(records),
            ]

            def invoke(options, flag):
                """正常なローカルrunを起動し得る引数組合せは使わない。"""
                env = {"PYTHONDONTWRITEBYTECODE": "1"}
                if flag is not None:
                    env["CDC_CONTRACT_EXPERIMENT"] = flag
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "infra/cdc/consumer.py"),
                        "--state-dir",
                        str(guarded),
                        *options,
                    ],
                    cwd=root,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertFalse(guarded.exists())
                self.assertEqual(result.stderr, "")
                self.assertNotIn(str(root), result.stdout)
                self.assertNotIn("fixture_db", result.stdout)
                return result.returncode, json.loads(result.stdout)

            for options, flag in (
                (arguments, None),
                (arguments, "false"),
                (arguments[:2], "true"),
                ([], "true"),
            ):
                result, receipt = invoke(options, flag)
                self.assertEqual(result, 1)
                self.assertIs(receipt["failed"], True)

            result, receipt = invoke(arguments, "true")
            self.assertEqual(result, 0)
            self.assertEqual(
                receipt, {"synthetic_only": True, "raw": 1, "quarantine": 0}
            )
            unknown = typed_delivery(0, after=DYNAMIC_ROW)
            unknown["topic"] = "fixture.privateexample.orders"
            records.write_text(json.dumps([unknown]), encoding="utf-8")
            result, receipt = invoke(arguments, "true")
            self.assertEqual(result, 1)
            self.assertIs(receipt["failed"], True)
            self.assertNotIn("privateexample", json.dumps(receipt))

    def test_replay_preserves_retain_delete_across_reverse_sorted_aliases(
        self,
    ):
        """後発DELETEがtopic辞書順で先でも、合成の旧値を保持する。"""
        table = {**DYNAMIC_TABLE, "delete_policy": "retain"}
        experiment = self.make_experiment(table=table)
        ordinary = typed_delivery(0, after=DYNAMIC_ROW)
        # 短いaliasのほうが辞書順では前。partitionも別bucketを選ぶ。
        alias = "fixture.example.orders"
        self.assertLess(alias, ordinary["topic"])
        experiment.process([ordinary])
        observed = experiment.rows("test_orders")[0]
        delete = typed_delivery(0, "d", before=DYNAMIC_ROW)
        payload = json.loads(delete["value"])
        payload["source"]["event_serial_no"] = 2
        delete.update(topic=alias, partition=1, value=json.dumps(payload))
        experiment.process([delete])
        tombstone = {**typed_delivery(1, None), "topic": alias, "partition": 1}
        experiment.process([tombstone])
        expected = [{"deleted": True, "row": observed}]
        for layer in ("current", "silver"):
            self.assertEqual(experiment.rows("test_orders", layer), expected)
        self.assertEqual(experiment.raw_count(), 3)
        replayed = experiment.replay()
        self.addCleanup(replayed.db.close)
        for layer in ("current", "silver"):
            self.assertEqual(replayed.rows("test_orders", layer), expected)
        self.assertEqual(replayed.raw_count(), 3)
        self.assertEqual(replayed.offset(DYNAMIC_TOPIC, 0), 0)
        self.assertEqual(replayed.offset(alias, 1), 1)

    def test_replay_uses_accepted_order_not_first_raw_append_order(self):
        """先に保存されたgap配送は未確定の間だけ除外し、再試行後に再生する。"""
        from contract_experiment import ContractBatchError

        table = {**DYNAMIC_TABLE, "delete_policy": "retain"}
        experiment = self.make_experiment(table=table)
        delete = typed_delivery(1, "d", before=DYNAMIC_ROW)
        with self.assertRaisesRegex(ContractBatchError, "OFFSET_GAP"):
            experiment.process([delete])
        self.assertEqual(experiment.raw_count(), 1)
        self.assertEqual(experiment.offset(DYNAMIC_TOPIC, 0), -1)
        experiment.process([typed_delivery(0, after=DYNAMIC_ROW)])
        expected = experiment.rows("test_orders")[0]
        incomplete = experiment.replay()
        self.addCleanup(incomplete.db.close)
        self.assertEqual(incomplete.raw_count(), 1)
        self.assertEqual(incomplete.rows("test_orders"), [expected])
        self.assertEqual(incomplete.offset(DYNAMIC_TOPIC, 0), 0)
        experiment.process([delete])
        self.assertEqual(experiment.raw_count(), 2)
        expected = [{"deleted": True, "row": expected}]
        replayed = experiment.replay()
        self.addCleanup(replayed.db.close)
        for layer in ("current", "silver"):
            self.assertEqual(experiment.rows("test_orders", layer), expected)
            self.assertEqual(replayed.rows("test_orders", layer), expected)
        self.assertEqual(replayed.raw_count(), 2)
        self.assertEqual(replayed.offset(DYNAMIC_TOPIC, 0), 1)
