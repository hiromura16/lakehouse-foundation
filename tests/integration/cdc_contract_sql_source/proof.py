"""隔離 SQL Server / Connect / Kafka の実際の合成配送を検査する。"""

import calendar
import json
import os
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

BROKER = "fixture-broker:9092"
CONNECT = "http://fixture-connect:8083"
SQL_HOST = "fixture-sql"
DATABASE = "fixture_normal_cdc"
TABLE = "fixture_orders"
LOGIN = "fixture_reader"
CONNECTOR = "fixture-source"
PREFIX = "fixture_source"
TOPIC = f"{PREFIX}.{DATABASE}.dbo.{TABLE}"
SHORT_ALIAS = f"{PREFIX}.dbo.{TABLE}"
HISTORY = "_fixture_schema_history"
STAMP0 = "2026-01-02T03:04:05.1234567"
STAMP1 = "2026-01-02T03:04:06.2345678"
STAMP2 = "2026-01-02T03:04:07.3456789"
KEY_FIELDS = ("site_id", "record_id")


class Failure(RuntimeError):
    """接続情報やレコードを含まない固定の試験エラー。"""


class NoRedirect(HTTPRedirectHandler):
    """内部 Connect の応答でも別アドレスへリダイレクトしない。"""

    def redirect_request(self, *args, **kwargs):
        """通信先を増やさない。"""
        return None


def require(condition, reason):
    """証明できないときは固定理由で停止する。"""
    if not condition:
        raise Failure(reason)


def nanos(stamp):
    """実行時のローカル時差や float を使わず、7桁を比較する。"""
    clock, fraction = stamp.split(".")
    seconds = calendar.timegm(time.strptime(clock, "%Y-%m-%dT%H:%M:%S"))
    return seconds * 1_000_000_000 + int(fraction) * 100


def image(key, label, qty, stamp):
    """CDC 実測と比較する唯一の合成期待値を作る。"""
    return {
        "site_id": "sample",
        "record_id": key,
        "label": label,
        "quantity": qty,
        "observed_at": nanos(stamp),
    }


SNAPSHOT = image(1, None, "2.500", STAMP0)
INSERTED = image(2, None, "12.340", STAMP1)
UPDATED = image(2, "", "-1.234", STAMP2)


def decode(value):
    """裸JSONの重複キー/非標準数値を受け付けない。"""

    def unique(pairs):
        result = {}
        for name, item in pairs:
            require(name not in result, "DUPLICATE_JSON_FIELD")
            result[name] = item
        return result

    def invalid(_):
        raise Failure("BAD_JSON")

    try:
        return json.loads(
            value, object_pairs_hook=unique, parse_constant=invalid
        )
    except (TypeError, ValueError, RecursionError):
        raise Failure("BAD_JSON") from None


def checkpoint(topic, partition, offset, previous, key, payload, op):
    """完全一致した業務キー、値、SQL source とKafka配送位置を検証する。"""
    require(
        topic == TOPIC
        and partition == 0
        and type(offset) is int
        and offset == previous + 1,
        "BROKER_COORDINATE",
    )
    expected = 1 if op == "r" else 2
    identity = decode(key)
    require(
        isinstance(identity, dict)
        and tuple(identity) == KEY_FIELDS
        and identity["site_id"] == "sample"
        and type(identity["record_id"]) is int
        and identity["record_id"] == expected,
        "KEY_CONTRACT",
    )
    if op == "tombstone":
        require(payload is None, "EXPECTED_KAFKA_NULL")
        return
    envelope = decode(payload)
    require(
        isinstance(envelope, dict)
        and {"before", "after", "source"} <= envelope.keys()
        and envelope.get("op") == op,
        "OPERATION_ORDER",
    )
    source = envelope.get("source")
    require(
        isinstance(source, dict)
        and (source.get("db"), source.get("schema"), source.get("table"))
        == (DATABASE, "dbo", TABLE),
        "SQL_SOURCE_IDENTITY",
    )
    require(
        source.get("connector") == "sqlserver"
        and source.get("version") == "3.7.1.Final",
        "CONNECTOR_SOURCE_VERSION",
    )
    if op == "r":
        require(
            source.get("snapshot") in ("true", "last")
            or source.get("snapshot") is True,
            "EXPECTED_INITIAL_SNAPSHOT",
        )
    else:
        for field in ("change_lsn", "commit_lsn"):
            lsn = source.get(field)
            require(
                isinstance(lsn, str)
                and re.fullmatch(
                    r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}:[0-9a-fA-F]{4}", lsn
                )
                and any(letter not in ":0" for letter in lsn),
                "SQL_CHANGE_POSITION",
            )
    before, after = {
        "r": (None, SNAPSHOT),
        "c": (None, INSERTED),
        "u": (INSERTED, UPDATED),
        "d": (UPDATED, None),
    }[op]
    for actual, wanted in (
        (envelope.get("before"), before),
        (envelope.get("after"), after),
    ):
        require(actual == wanted, "TYPED_ROW_IMAGE")
        if wanted is not None:
            require(
                type(actual["observed_at"]) is int
                and type(actual["record_id"]) is int,
                "EXPECTED_EXACT_INTEGERS",
            )


class Proof:
    """外部パラメータを受け取らない使い捨ての一表試験。"""

    def __init__(self, sql, kafka, admin_client, secret, reader):
        """依存ライブラリは hosted entry point だけで取り込む。"""
        self.sql, self.kafka = sql, kafka
        self.admin_client = admin_client
        self.secret, self.reader = secret, reader
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.phase = "SQL_START"
        self.offset = -1

    def connect(self, database, *, reader=False):
        """暗号化・CAと短い接続/SQL期限を持つ専用SQLセッション。"""
        return self.sql.connect(
            server=SQL_HOST,
            port="1433",
            database=database,
            user=LOGIN if reader else "sa",
            password=self.reader if reader else self.secret,
            encryption="require",
            autocommit=True,
            charset="UTF-8",
            login_timeout=3,
            timeout=10,
        )

    def await_sql(self):
        """SQL2022 と実際の現行 Agent 稼働を確認する。"""
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            try:
                with self.connect("master") as session:
                    cur = session.cursor()
                    cur.execute(
                        "SELECT DB_NAME(),CONVERT(int,SERVERPROPERTY("
                        "'ProductMajorVersion')),"
                        "CASE WHEN EXISTS(SELECT 1 FROM "
                        "sys.dm_server_services "
                        "WHERE servicename LIKE N'SQL Server Agent%' "
                        "AND status=4) THEN 1 ELSE 0 END"
                    )
                    row = cur.fetchone()
                    require(row[:2] == ("master", 16), "SQL_INSTANCE_IDENTITY")
                    if row[2] == 1:
                        return
            except self.sql.Error:
                pass
            time.sleep(1)
        raise Failure("SQL_OR_AGENT_NOT_READY")

    def execute(self, database, statement, args=(), *, reader=False):
        """このfixtureが作ったDBまたは新規サーバーのmasterだけを使う。"""
        require(database in {"master", DATABASE}, "WRONG_SQL_TARGET")
        with self.connect(database, reader=reader) as session:
            cur = session.cursor()
            cur.execute(statement, args)
            return cur.fetchall() if cur.description else ()

    def provision(self):
        """実名や既存表に触れず、新規DB・1表・1capture・限定readerを準備。"""
        require(
            self.execute(
                "master", "SELECT DB_ID(%s),SUSER_ID(%s)", (DATABASE, LOGIN)
            )
            == [(None, None)],
            "FIXTURE_RESOURCES_EXIST",
        )
        self.execute("master", "CREATE DATABASE [" + DATABASE + "]")
        self.execute(
            "master",
            "CREATE LOGIN [" + LOGIN + "] WITH PASSWORD=%s",
            (self.reader,),
        )
        self.execute(
            "master", "GRANT VIEW SERVER SECURITY STATE TO [" + LOGIN + "]"
        )
        self.execute(
            DATABASE, "CREATE USER [" + LOGIN + "] FOR LOGIN [" + LOGIN + "]"
        )
        self.execute(DATABASE, "CREATE ROLE [fixture_cdc_reader]")
        self.execute(
            DATABASE,
            "ALTER ROLE [fixture_cdc_reader] ADD MEMBER [" + LOGIN + "]",
        )
        self.execute(DATABASE, "EXEC sys.sp_cdc_enable_db")
        self.execute(
            DATABASE,
            "CREATE TABLE [dbo].[fixture_orders] ("
            "site_id nvarchar(20) NOT NULL,record_id int NOT NULL,"
            "label nvarchar(40) NULL,quantity decimal(18,3) NOT NULL,"
            "observed_at datetime2(7) NOT NULL,"
            "CONSTRAINT pk_fixture_orders PRIMARY KEY(site_id,record_id))",
        )
        self.execute(
            DATABASE,
            "EXEC sys.sp_cdc_enable_table "
            "@source_schema=N'dbo',@source_name=N'fixture_orders',"
            "@role_name=N'fixture_cdc_reader',@supports_net_changes=0,"
            "@allow_partition_switch=0",
        )
        self.execute(
            DATABASE,
            "GRANT SELECT ON dbo.fixture_orders TO [fixture_cdc_reader]",
        )
        for name in (
            "change_tables",
            "captured_columns",
            "lsn_time_mapping",
            "dbo_fixture_orders_CT",
        ):
            self.execute(
                DATABASE,
                "GRANT SELECT ON [cdc].[" + name + "] TO [fixture_cdc_reader]",
            )
        self.execute(DATABASE, "GRANT VIEW DATABASE STATE TO [" + LOGIN + "]")
        self.insert(1, "2.500", STAMP0)
        require(
            self.execute(
                DATABASE,
                "SELECT COUNT_BIG(*) FROM dbo.fixture_orders",
                reader=True,
            )
            == [(1,)],
            "READER_OR_SNAPSHOT_SEED",
        )

    def insert(self, key, amount, stamp):
        """真正の個別コミットを初期値と CDC 追跡用に発生させる。"""
        self.execute(
            DATABASE,
            "INSERT dbo.fixture_orders VALUES "
            "(N'sample',%s,NULL,CONVERT(decimal(18,3),%s),"
            "CONVERT(datetime2(7),%s))",
            (key, amount, stamp),
        )

    def http(self, path, data=None):
        """固定 Connect エンドポイントに限定してJSONを授受する。"""
        body = None if data is None else json.dumps(data).encode()
        request = Request(
            CONNECT + path,
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def register(self):
        """他の connector がない新規workerに正式版の1設定だけを作成。"""
        deadline = time.monotonic() + 90
        while True:
            try:
                initial = self.http("/connectors")
                break
            except (URLError, TimeoutError) as error:
                if (
                    isinstance(error, HTTPError)
                    or time.monotonic() >= deadline
                ):
                    raise Failure("CONNECT_NOT_READY") from None
                time.sleep(1)
        require(initial == [], "CONNECT_NOT_EMPTY")
        plugins = self.http("/connector-plugins")
        classname = "io.debezium.connector.sqlserver.SqlServerConnector"
        require(
            any(
                x.get("class") == classname
                and x.get("version") == "3.7.1.Final"
                for x in plugins
            ),
            "DEBEZIUM_PLUGIN_VERSION",
        )
        envref = "$" + "{env:FIXTURE_SQL_READER_PASSWORD}"
        schema_scope = "schema.history.internal.store.only.captured."
        converter = "org.apache.kafka.connect.json.JsonConverter"
        result = self.http(
            "/connectors",
            {
                "name": CONNECTOR,
                "config": {
                    "connector.class": classname,
                    "tasks.max": "1",
                    "database.hostname": SQL_HOST,
                    "database.port": "1433",
                    "database.names": DATABASE,
                    "database.user": LOGIN,
                    "database.password": envref,
                    "topic.prefix": PREFIX,
                    "table.include.list": r"dbo\.fixture_orders",
                    "schema.history.internal.kafka.bootstrap.servers": BROKER,
                    "schema.history.internal.kafka.topic": HISTORY,
                    schema_scope + "tables.ddl": "true",
                    schema_scope + "databases.ddl": "true",
                    "data.query.mode": "direct",
                    "snapshot.mode": "initial",
                    "snapshot.isolation.mode": "exclusive",
                    "include.schema.changes": "false",
                    "tombstones.on.delete": "true",
                    "decimal.handling.mode": "string",
                    "time.precision.mode": "adaptive",
                    "driver.encrypt": "true",
                    "driver.trustServerCertificate": "false",
                    "driver.trustStore": "/fixture/trust.p12",
                    "driver.trustStorePassword": "fixture-ca-only",
                    "driver.trustStoreType": "PKCS12",
                    "key.converter": converter,
                    "key.converter.schemas.enable": "false",
                    "value.converter": converter,
                    "value.converter.schemas.enable": "false",
                },
            },
        )
        require(result.get("name") == CONNECTOR, "CONNECTOR_CREATE")

    def live_task(self):
        """認識した職務が1個だけで、Connect/Task が実際にRUNNING。"""
        body = self.http(f"/connectors/{CONNECTOR}/status")
        status = body.get("connector", {}).get("state")
        tasks = body.get("tasks", [])
        require(
            status != "FAILED"
            and all(t.get("state") != "FAILED" for t in tasks),
            "CONNECTOR_TASK_FAILED",
        )
        return (
            status == "RUNNING"
            and len(tasks) == 1
            and (tasks[0].get("id"), tasks[0].get("state")) == (0, "RUNNING")
        )

    def evidence(self, consumer, op):
        """先着メッセージを検証。timeoutで欠落を成功扱いしない。"""
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                self.live_task()
                continue
            require(message.error() is None, "KAFKA_MESSAGE_ERROR")
            checkpoint(
                message.topic(),
                message.partition(),
                message.offset(),
                self.offset,
                message.key(),
                message.value(),
                op,
            )
            self.offset = message.offset()
            return message
        raise Failure("KAFKA_DELIVERY_TIMEOUT")

    def run(self):
        """snapshot を観測してから、別トランザクションの変更を発生させる。"""
        self.await_sql()
        self.phase = "SQL_PROVISION"
        self.provision()
        self.phase = "CONNECT_REGISTER"
        self.register()
        self.phase = "KAFKA_SNAPSHOT"
        consumer = self.kafka.Consumer(
            {
                "bootstrap.servers": BROKER,
                "group.id": "fixture-proof-only",
                "enable.auto.commit": False,
                "allow.auto.create.topics": False,
                "log_level": 0,
            }
        )
        try:
            consumer.assign([self.kafka.TopicPartition(TOPIC, 0, 0)])
            self.evidence(consumer, "r")
            self.changes(consumer)
            self.verify_final(consumer)
        finally:
            consumer.close()

    def changes(self, consumer):
        """初回 snapshot の後だけ、別行に I/U/D を配送する。"""
        self.phase = "KAFKA_INSERT"
        self.insert(2, "12.340", STAMP1)
        self.evidence(consumer, "c")
        self.phase = "KAFKA_UPDATE"
        self.execute(
            DATABASE,
            "UPDATE dbo.fixture_orders SET label=N'',"
            "quantity=-1.234,observed_at=CONVERT(datetime2(7),%s) "
            "WHERE site_id=N'sample' AND record_id=2",
            (STAMP2,),
        )
        self.evidence(consumer, "u")
        self.phase = "KAFKA_DELETE_AND_TOMBSTONE"
        self.execute(
            DATABASE,
            "DELETE dbo.fixture_orders "
            "WHERE site_id=N'sample' AND record_id=2",
        )
        deletion = self.evidence(consumer, "d")
        self.evidence(consumer, "tombstone")
        return deletion

    def verify_final(self, consumer):
        """厳密な broker 配送数と生き残る元のSQL行を検査する。"""
        self.phase = "FINAL_BROKER_AND_SQL"
        topics = self.admin_client({"bootstrap.servers": BROKER}).list_topics(
            timeout=5
        )
        require(SHORT_ALIAS not in topics.topics, "ALTERNATE_TOPIC_PRESENT")
        require(
            set(topics.topics[TOPIC].partitions) == {0}
            and set(topics.topics[HISTORY].partitions) == {0}
            and consumer.get_watermark_offsets(
                self.kafka.TopicPartition(TOPIC, 0), timeout=5
            )
            == (0, 5)
            and self.live_task(),
            "FINAL_KAFKA_INVENTORY",
        )
        require(
            self.execute(
                DATABASE,
                "SELECT record_id FROM dbo.fixture_orders",
                reader=True,
            )
            == [(1,)],
            "FINAL_SQL_STATE",
        )


def validate_hosted():
    """既存の正式 host guard を両方のエントリで使う。"""
    require(
        os.environ.get("CDC_SQL_SOURCE_HOSTED") == "true"
        and re.fullmatch(
            r"normal-cdc-[0-9a-f]{24}",
            os.environ.get("CDC_SQL_SOURCE_ID", ""),
        )
        and os.environ.get("FREETDSCONF") == "/fixture/freetds.conf",
        "DISPOSABLE_HOSTED_REQUIRED",
    )


def fixture_passwords():
    """生成した異なる2アカウントの fixture secrets のみ。"""
    secret, reader = (
        os.environ.get(key, "")
        for key in (
            "FIXTURE_SQL_ADMIN_PASSWORD",
            "FIXTURE_SQL_READER_PASSWORD",
        )
    )
    require(
        all(re.fullmatch(r"Aa9![0-9a-f]{48}", v) for v in (secret, reader))
        and secret != reader,
        "FIXTURE_PASSWORD_FORMAT",
    )
    return secret, reader


def main():
    """端末や実運用では動作しない。失敗時は本文・認証を通知しない。"""
    phase = "ENTRY"
    proof = None
    try:
        validate_hosted()
        secret, reader = fixture_passwords()
        import confluent_kafka
        import pymssql
        from confluent_kafka.admin import AdminClient

        proof = Proof(pymssql, confluent_kafka, AdminClient, secret, reader)
        proof.run()
    except Failure as error:
        print(
            f"FAIL: {proof.phase if proof else phase}/{error}", file=sys.stderr
        )
        return 1
    except Exception:
        print(
            "FAIL: " + (proof.phase if proof else phase) + "/RUNTIME",
            file=sys.stderr,
        )
        return 1
    print(
        "SQL_SOURCE_KAFKA=pass snapshot=1 insert=1 update=1 "
        "delete=1 tombstone=1"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
