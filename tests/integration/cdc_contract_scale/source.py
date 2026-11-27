"""所有済みの隔離runnerでだけ使う、実SQL→Debezium→Kafkaの合成5表driver。

将来の使い捨てrunnerで、新規内部brokerだけを先に起動して
prepare-topics を実行する。SQLとConnectを起動した後に run を実行。
Connectの固定Config/Offset/Status名には INTERNAL_TOPICS を設定する。
同じ合成Kafka cluster ID、固定内部名、reader TLS、rootless client、
ポート非公開はrunnerの所有検査が別途必須。端末でのdocker作業や
既存topic/DBの再利用は不可。run はsourceのみ。別のchain laneは同じ
受信clientで隔離Icebergの保存とACKを照合し、再起動や現場利用は含まない。
"""

import importlib
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice
from pathlib import Path

import fixture

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "cdc_contract_sql_source")
)
base = importlib.import_module("proof")

SCALE_ROLE = "fixture_scale_reader"
CONNECTOR = "fixture-scale-source"
HISTORY = "_fixture_scale_schema_history"
INTERNAL_TOPICS = (
    "_fixture_scale_connect_configs",
    "_fixture_scale_connect_offsets",
    "_fixture_scale_connect_status",
)
CLASS = "io.debezium.connector.sqlserver.SqlServerConnector"
VERSION = "3.7.1.Final"
SOURCE_DEADLINE = 150
SOURCE_GROUP = "fixture-scale-source-only"
CHAIN_GROUP = "fixture-scale-wire-chain"


def create_topic_plan():
    """data/historyをcompactせず、内部Connect topicと物理名も分ける。"""
    plan = {name: {"cleanup.policy": "compact"} for name in INTERNAL_TOPICS}
    for name in (HISTORY, *(spec.topic for spec in fixture.TABLES)):
        plan[name] = {
            "cleanup.policy": "delete",
            "retention.ms": "-1",
            "retention.bytes": "-1",
        }
    return plan


def configuration():
    """公開説明や資格情報を含まず、同一SQL fixtureの正確な5表だけを選ぶ。"""
    database = fixture.DATABASE
    overrides = "snapshot.select.statement.overrides"
    qualified = [f"{database}.dbo.{spec.name}" for spec in fixture.TABLES]
    converter = "org.apache.kafka.connect.json.JsonConverter"
    return {
        "connector.class": CLASS,
        "tasks.max": "1",
        "database.hostname": base.SQL_HOST,
        "database.port": "1433",
        "database.names": database,
        "database.user": base.LOGIN,
        "database.password": "$" + "{env:FIXTURE_SQL_READER_PASSWORD}",
        "topic.prefix": fixture.PREFIX,
        "table.include.list": ",".join(
            r"dbo\." + spec.name for spec in fixture.TABLES
        ),
        "schema.history.internal.kafka.bootstrap.servers": base.BROKER,
        "schema.history.internal.kafka.topic": HISTORY,
        "schema.history.internal.store.only.captured.tables.ddl": "true",
        "schema.history.internal.store.only.captured.databases.ddl": "true",
        "data.query.mode": "direct",
        "snapshot.mode": "initial",
        "snapshot.isolation.mode": "exclusive",
        "snapshot.max.threads": "1",
        overrides: ",".join(qualified),
        **{
            f"{overrides}.{name}": (
                "SELECT site_id, record_id, label, quantity, observed_at FROM "
                + f"[{database}]."
                + fixture.table_identifier(spec)
                + " ORDER BY site_id, record_id"
            )
            for name, spec in zip(qualified, fixture.TABLES, strict=True)
        },
        "decimal.handling.mode": "string",
        "time.precision.mode": "adaptive",
        "tombstones.on.delete": "true",
        "include.schema.changes": "false",
        "driver.encrypt": "true",
        "driver.trustServerCertificate": "false",
        "driver.trustStore": "/fixture/trust.p12",
        "driver.trustStorePassword": "fixture-ca-only",
        "driver.trustStoreType": "PKCS12",
        "key.converter": converter,
        "key.converter.schemas.enable": "false",
        "value.converter": converter,
        "value.converter.schemas.enable": "false",
    }


def plans():
    """期待配送順をtopic別に保持し、global Kafka offsetを作らない。"""
    return {spec.topic: tuple(fixture.steps(spec)) for spec in fixture.TABLES}


def seeds():
    """全表を最初に作り、初期snapshotの112行だけを発行する。"""
    for spec in fixture.TABLES:
        for i, step in enumerate(fixture.steps(spec)):
            if i == spec.initial:
                break
            yield spec, step


def mutations():
    """固定1024 SQL操作を表ごとに交互に進める。tombstoneは発行しない。"""
    plan = plans()
    active = {
        spec: islice(plan[spec.topic], spec.initial, None)
        for spec in fixture.TABLES
    }
    while active:
        retained = {}
        for spec, events in active.items():
            event = next(events, None)
            if event is not None:
                retained[spec] = events
                if event.op != "tombstone":
                    yield spec, event
        active = retained


INITIAL_SNAPSHOT_FLAGS = {
    "first": "FIRST",
    "first_in_data_collection": "TABLE_FIRST",
    "true": "INTERIOR",
    "last_in_data_collection": "TABLE_LAST",
    "last": "LAST",
}


def marker_class(marker):
    """固定診断語だけ。任意のフィールド文字列を例外に含めない。"""
    if marker is None:
        return "MISSING"
    if type(marker) is bool:
        return "BOOLEAN"
    if type(marker) is str:
        return {
            **INITIAL_SNAPSHOT_FLAGS,
            "false": "STREAM",
            "incremental": "INCREMENTAL",
        }.get(marker, "OTHER")
    return "OTHER"


def verify_snapshot_marker(marker, position, table_size):
    """3.7.1のSnapshotMarkerResolverどおりに表内の端を検査する。"""
    base.require(
        type(position) is int
        and type(table_size) is int
        and 0 <= position < table_size
        and table_size > 1,
        "SCALE_SNAPSHOT_POSITION",
    )
    if position == 0:
        allowed = ("first", "first_in_data_collection")
    elif position == table_size - 1:
        allowed = ("last", "last_in_data_collection")
    else:
        allowed = ("true",)
    base.require(
        type(marker) is str and marker in allowed,
        "SCALE_SNAPSHOT_FLAG_" + marker_class(marker),
    )


def verify_snapshot_fence(records):
    """各表の既知の112件を越えて、全体snapshotの両端も実証する。"""
    specs = {spec.topic: spec for spec in fixture.TABLES}
    markers = Counter()
    global_first = []
    global_last = []
    for record in records:
        spec = specs[record["topic"]]
        marker = base.decode(record["value"])["source"].get("snapshot")
        verify_snapshot_marker(marker, record["offset"], spec.initial)
        markers[marker] += 1
        if marker == "first":
            global_first.append(spec.topic)
        elif marker == "last":
            global_last.append(spec.topic)
    tables = len(specs)
    demand_count = sum(t.initial for t in specs.values())
    base.require(
        markers
        == {
            "first": 1,
            "first_in_data_collection": tables - 1,
            "true": demand_count - 2 * tables,
            "last_in_data_collection": tables - 1,
            "last": 1,
        }
        and global_first != global_last,
        "SCALE_SNAPSHOT_GLOBAL_FENCE",
    )


def verify_origin(record, operation, *, position=None, table_size=None):
    """合成期待値に加え実ConnectorとSQL log positionを別に検査。"""
    if operation == "tombstone":
        base.require(record["value"] is None, "SCALE_TOMBSTONE_NOT_NULL")
        return
    origin = base.decode(record["value"])["source"]
    base.require(
        origin.get("connector") == "sqlserver"
        and origin.get("version") == VERSION,
        "SCALE_SOURCE_PLUGIN_IDENTITY",
    )
    snapshot = origin.get("snapshot")
    if operation == "r":
        verify_snapshot_marker(snapshot, position, table_size)
        return
    base.require(
        snapshot is None or snapshot is False or snapshot == "false",
        "SCALE_STREAM_SNAPSHOT_FLAG_" + marker_class(snapshot),
    )
    for field in ("commit_lsn", "change_lsn"):
        candidate = origin.get(field)
        base.require(
            type(candidate) is str
            and re.fullmatch(
                r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}:[0-9a-fA-F]{4}",
                candidate,
            )
            and set(candidate) - {"0", ":"},
            "SCALE_STREAMING_LOG_POSITION",
        )


@dataclass(frozen=True, slots=True)
class NativePort:
    """実際のconsumerに所有Kafkaと選択済みgroupを束縛する。"""

    client: object
    topic_partition: object
    connector: object
    group: str

    def identity(self):
        """同じrunnerとclusterであることをKafkaへ再確認する。"""
        return self.connector.metadata().cluster_id, self.group

    def watermarks(self, topics):
        """受信した全5partitionのlow/highをキャッシュなしで再読。"""
        return {
            topic: self.client.get_watermark_offsets(
                self.topic_partition(topic, 0), timeout=5, cached=False
            )
            for topic in sorted(topics)
        }


class Scale(base.Proof):
    """接続先・DBの固定名を増やさず、fixtureの中だけでSQLとKafkaを操作。"""

    def __init__(
        self, sql, kafka, admin_client, topic_class, cluster, secret, reader
    ):
        """callerから渡るのは所有済みbrokerの照合値のみ。"""
        super().__init__(sql, kafka, admin_client, secret, reader)
        self.phase = "SCALE_ENTRY"
        self.topic_class = topic_class
        self.cluster = cluster
        self.admin = admin_client({"bootstrap.servers": base.BROKER})

    def metadata(self):
        """ブローカーがランナー作成時の合成cluster IDと違えば停止。"""
        document = self.admin.list_topics(timeout=6)
        base.require(
            document.cluster_id == self.cluster, "SCALE_BROKER_IDENTITY"
        )
        return document

    def prepare_topics(self):
        """空のfixture brokerを確認してから専用topicだけを1回作る。"""
        document = self.metadata()
        base.require(not document.topics, "SCALE_BROKER_NOT_EMPTY")
        plan = create_topic_plan()
        listed = [
            self.topic_class(name, 1, 1, config=settings)
            for name, settings in plan.items()
        ]
        results = self.admin.create_topics(listed, operation_timeout=15)
        base.require(set(results) == set(plan), "SCALE_TOPIC_CREATE_SET")
        for outcome in results.values():
            outcome.result(timeout=16)
        self.topic_inventory()

    def topic_inventory(self):
        """post-createも別の名称/partition/エラーを許さない。"""
        topics = self.metadata().topics
        allowed = set(create_topic_plan())
        base.require(
            allowed <= set(topics) <= allowed | {"__consumer_offsets"},
            "SCALE_TOPIC_SET",
        )
        base.require(
            all(
                topics[t].error is None
                and set(topics[t].partitions) == {0}
                and topics[t].partitions[0].error is None
                for t in allowed
            ),
            "SCALE_TOPIC_PARTITION_SET",
        )

    def execute(self, database, statement, args=(), *, reader=False):
        """登録されている実験DBと空のSQL master以外へは入らない。"""
        base.require(
            database in {"master", fixture.DATABASE}, "SCALE_WRONG_SQL_TARGET"
        )
        with self.connect(database, reader=reader) as session:
            cur = session.cursor()
            cur.execute(statement, args)
            return cur.fetchall() if cur.description else ()

    def provision(self):
        """新しい合成DB・5表・厳密なcaptureと読取ロールだけを準備。"""
        database = fixture.DATABASE
        login = base.LOGIN
        base.require(
            self.execute(
                "master", "SELECT DB_ID(%s),SUSER_ID(%s)", (database, login)
            )
            == [(None, None)],
            "SCALE_SQL_ALREADY_EXISTS",
        )
        self.execute("master", "CREATE DATABASE [" + database + "]")
        self.execute(
            "master",
            "CREATE LOGIN [" + login + "] WITH PASSWORD=%s",
            (self.reader,),
        )
        self.execute(
            "master", "GRANT VIEW SERVER SECURITY STATE TO [" + login + "]"
        )
        self.execute(
            database, "CREATE USER [" + login + "] FOR LOGIN [" + login + "]"
        )
        self.execute(database, "CREATE ROLE [" + SCALE_ROLE + "]")
        self.execute(
            database,
            "ALTER ROLE [" + SCALE_ROLE + "] ADD MEMBER [" + login + "]",
        )
        self.execute(database, "EXEC sys.sp_cdc_enable_db")
        self.execute(database, "GRANT VIEW DATABASE STATE TO [" + login + "]")
        for spec in fixture.TABLES:
            ident = fixture.table_identifier(spec)
            self.execute(
                database,
                "CREATE TABLE " + ident + " ("
                "site_id nvarchar(20) NOT NULL,record_id int NOT NULL,"
                "label nvarchar(40) NULL,quantity decimal(18,3) NOT NULL,"
                "observed_at datetime2(7) NOT NULL,"
                "PRIMARY KEY(site_id,record_id))",
            )
            self.execute(
                database,
                "EXEC sys.sp_cdc_enable_table "
                "@source_schema=N'dbo',@source_name=%s,@role_name=%s,"
                "@supports_net_changes=0,@allow_partition_switch=0",
                (spec.name, SCALE_ROLE),
            )
            self.execute(
                database,
                "GRANT SELECT ON " + ident + " TO [" + SCALE_ROLE + "]",
            )
            self.execute(
                database,
                "GRANT SELECT ON [cdc].[dbo_"
                + spec.name
                + "_CT] TO ["
                + SCALE_ROLE
                + "]",
            )
        for name in ("change_tables", "captured_columns", "lsn_time_mapping"):
            self.execute(
                database,
                "GRANT SELECT ON [cdc].[" + name + "] TO [" + SCALE_ROLE + "]",
            )
        tracked = self.execute(
            database,
            "SELECT name FROM sys.tables WHERE is_tracked_by_cdc=1",
            reader=True,
        )
        base.require(
            len(tracked) == len(fixture.TABLES)
            and {row[0] for row in tracked}
            == {spec.name for spec in fixture.TABLES},
            "SCALE_CDC_TABLE_SET",
        )

    def write_sql(self, changes, expected):
        """単一の専用接続で各DMLを独立commitし、各行1件を実確認する。"""
        count = 0
        with self.connect(fixture.DATABASE) as session:
            cur = session.cursor()
            cur.execute("SET NOCOUNT ON")
            for spec, step in changes:
                stmt, values = fixture.sql_step(spec, step)
                cur.execute(stmt + "; SELECT CONVERT(int,@@ROWCOUNT)", values)
                base.require(
                    cur.fetchall() == [(1,)], "SCALE_SQL_AFFECTED_ROWS"
                )
                count += 1
        base.require(count == expected, "SCALE_SQL_CHANGE_COUNT")

    def seed_captured(self):
        """全seed行がSQL Agentに実捕捉されるまではsnapshotを始めない。"""
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            counts = []
            for spec in fixture.TABLES:
                data = self.execute(
                    fixture.DATABASE,
                    "SELECT COUNT_BIG(*) FROM [cdc].[dbo_"
                    + spec.name
                    + "_CT] "
                    "WHERE [__$operation]=2 AND site_id=N'synthetic' "
                    "AND [__$start_lsn] IS NOT NULL "
                    "AND [__$start_lsn]<>0x00000000000000000000",
                    reader=True,
                )
                base.require(
                    len(data) == 1 and 0 <= data[0][0] <= spec.initial,
                    "SCALE_CDC_SEED_COUNT",
                )
                counts.append(data[0][0])
            if counts == [spec.initial for spec in fixture.TABLES]:
                return
            time.sleep(1)
        raise base.Failure("SCALE_SQL_AGENT_NOT_CAPTURED")

    def register(self):
        """同じ所有workerに正式SQLプラグイン1つを機械設定する。"""
        self.connect_identity()
        base.require(
            self.connect_readiness("/connectors") == [],
            "SCALE_CONNECT_NOT_EMPTY",
        )
        plugins = self.connect_readiness("/connector-plugins")
        base.require(
            sum(
                item.get("class") == CLASS and item.get("version") == VERSION
                for item in plugins
            )
            == 1,
            "SCALE_PLUGIN_VERSION",
        )
        response = self.http(
            "/connectors", {"name": CONNECTOR, "config": configuration()}
        )
        base.require(response.get("name") == CONNECTOR, "SCALE_CONNECT_CREATE")

    def live(self):
        """実connection/taskが一つだけRUNNINGかを内部RESTから確認。"""
        value = self.http(f"/connectors/{CONNECTOR}/status")
        status = value.get("connector", {}).get("state")
        tasks = value.get("tasks", [])
        base.require(
            status != "FAILED"
            and all(t.get("state") != "FAILED" for t in tasks),
            "SCALE_CONNECT_TASK_FAILED",
        )
        return (
            status == "RUNNING"
            and len(tasks) == 1
            and (tasks[0].get("id"), tasks[0].get("state")) == (0, "RUNNING")
        )

    def receive(self, consumer, offsets, end, expected):
        """どの表から来ても、その表自身の厳密な次の型付き配送だけを許す。"""
        from consumer import message_record

        specs = {t.topic: t for t in fixture.TABLES}
        consumer.assign(
            [
                self.kafka.TopicPartition(topic, 0, offsets[topic])
                for topic in offsets
            ]
        )
        observed = []
        deadline = time.monotonic() + SOURCE_DEADLINE
        while offsets != end and time.monotonic() < deadline:
            message = consumer.poll(0.5)
            if message is None:
                self.live()
                continue
            base.require(message.error() is None, "SCALE_KAFKA_DELIVERY")
            record = message_record(message)
            topic = record["topic"]
            base.require(
                topic in end and offsets[topic] < end[topic],
                "SCALE_UNEXPECTED_TOPIC_OR_EXTRA",
            )
            current = offsets[topic]
            step = expected[topic][current]
            fixture.verify_record(record, specs[topic], current, step)
            verify_origin(
                record,
                step.op,
                position=current,
                table_size=specs[topic].initial,
            )
            observed.append(record)
            offsets[topic] += 1
        base.require(offsets == end, "SCALE_NATIVE_DELIVERY_MISSING")
        for topic, endpoint in end.items():
            coordinate = self.kafka.TopicPartition(topic, 0)
            base.require(
                consumer.get_watermark_offsets(coordinate, timeout=6)
                == (0, endpoint),
                "SCALE_WATERMARK_MISMATCH",
            )
        return observed

    def final_sql(self):
        """最終SQL行をKafkaまたは実験Consumerの状態とは別に照合。"""
        for spec in fixture.TABLES:
            results = self.execute(
                fixture.DATABASE,
                "SELECT site_id,record_id,label,quantity,"
                "CONVERT(varchar(27),observed_at,126) FROM "
                + fixture.table_identifier(spec)
                + " ORDER BY site_id,record_id",
                reader=True,
            )
            fields = (
                "site_id",
                "record_id",
                "label",
                "quantity",
                "observed_at",
            )
            converted = []
            for row in results:
                base.require(
                    type(row[1]) is int
                    and type(row[3]) is Decimal
                    and row[3].as_tuple().exponent == -3,
                    "SCALE_FINAL_SQL_TYPE",
                )
                converted.append(
                    dict(
                        zip(
                            fields,
                            (*row[:3], str(row[3]), row[4]),
                            strict=True,
                        )
                    )
                )
            base.require(
                converted == fixture.final_rows(spec), "SCALE_FINAL_SQL_ROWS"
            )

    def run(self, *, after_reconciled=None):
        """native source検証後、明示モードだけ同じconsumerで保存へ渡す。"""
        self.phase = "SCALE_BROKER"
        self.topic_inventory()
        self.connect_identity()
        base.require(
            self.connect_readiness("/connectors") == [],
            "SCALE_CONNECT_NOT_EMPTY",
        )
        self.phase = "SCALE_SQL"
        self.await_sql()
        self.provision()
        self.write_sql(seeds(), 112)
        self.seed_captured()
        self.phase = "SCALE_CONNECT"
        self.register()
        group = SOURCE_GROUP if after_reconciled is None else CHAIN_GROUP
        consumer = self.kafka.Consumer(
            {
                "bootstrap.servers": base.BROKER,
                "group.id": group,
                "enable.auto.commit": False,
                "enable.auto.offset.store": False,
                "allow.auto.create.topics": False,
                "log_level": 0,
            }
        )
        expected = plans()
        current = dict.fromkeys(expected, 0)
        try:
            self.phase = "SCALE_NATIVE_SNAPSHOT"
            native = self.receive(
                consumer,
                current,
                {spec.topic: spec.initial for spec in fixture.TABLES},
                expected,
            )
            self.phase = "SCALE_NATIVE_SNAPSHOT_FENCE"
            verify_snapshot_fence(native)
            self.phase = "SCALE_SQL_IUD"
            self.write_sql(mutations(), 1024)
            self.phase = "SCALE_NATIVE_CHANGES"
            native.extend(
                self.receive(
                    consumer,
                    current,
                    {topic: len(p) for topic, p in expected.items()},
                    expected,
                )
            )
            base.require(self.live(), "SCALE_FINAL_TASK_NOT_RUNNING")
            self.topic_inventory()
            self.phase = "SCALE_SQL_RECONCILE"
            self.final_sql()
            self.phase = "SCALE_CONTRACT_RECONCILE"
            result = fixture.reconcile(native)
            if after_reconciled is None:
                return result
            self.phase = "SCALE_CHAIN_STORAGE"
            port = NativePort(consumer, self.kafka.TopicPartition, self, group)
            return after_reconciled(native, port.watermarks(expected), port)
        finally:
            consumer.close()


def require_hosted():
    """既存のhosted guardを必須にし、未接続の段階ではnetworkを開かない。"""
    base.validate_hosted()
    cluster = os.environ.get("CDC_SQL_SCALE_KAFKA_ID", "")
    base.require(
        os.environ.get("CDC_SQL_SCALE_HOSTED") == "true"
        and re.fullmatch(r"[A-Za-z0-9_-]{22}", cluster),
        "SCALE_OWNED_RUNNER_REQUIRED",
    )
    return cluster


def main():
    """公開ログは固定の値と成否だけで、行・秘密・識別子を送らない。"""
    driver = None
    try:
        cluster = require_hosted()
        base.require(
            len(sys.argv) == 2
            and sys.argv[1] in {"prepare-topics", "run", "run-chain"},
            "SCALE_PHASE_REQUIRED",
        )
        mode = sys.argv[1]
        base.require(
            mode != "run-chain"
            or os.environ.get("CDC_SQL_SCALE_CHAIN") == "true",
            "SCALE_CHAIN_MODE_REQUIRED",
        )
        import confluent_kafka
        from confluent_kafka.admin import AdminClient, NewTopic

        sql = None
        secret, reader = "", ""
        if mode in {"run", "run-chain"}:
            secret, reader = base.fixture_passwords()
            sql = importlib.import_module("pymssql")
        driver = Scale(
            sql,
            confluent_kafka,
            AdminClient,
            NewTopic,
            cluster,
            secret,
            reader,
        )
        if mode == "prepare-topics":
            driver.phase = "SCALE_BROKER"
            driver.prepare_topics()
            print("SQL_SCALE_TOPICS=pass")
            return 0
        if mode == "run-chain":
            chain = importlib.import_module("chain")
            result = driver.run(after_reconciled=chain.native)
        else:
            result = driver.run()
    except base.Failure as error:
        print(
            f"FAIL: {driver.phase if driver else 'SCALE_ENTRY'}/{error}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print(
            f"FAIL: {driver.phase if driver else 'SCALE_ENTRY'}/RUNTIME",
            file=sys.stderr,
        )
        return 1
    mode_marker = (
        "SQL_SCALE_CHAIN=pass"
        if mode == "run-chain"
        else "SQL_SCALE_NATIVE=pass"
    )
    print(mode_marker + " " + " ".join(f"{k}={v}" for k, v in result.items()))
    return 0


if __name__ == "__main__":
    # chainがimportした時に別の型/Consumerを作らない。
    sys.modules["source"] = sys.modules[__name__]
    raise SystemExit(main())
