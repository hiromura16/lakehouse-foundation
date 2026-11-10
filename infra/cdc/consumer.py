"""Kafkaの変更を、raw履歴・現在値・CDC分析表へ連続反映する。

読む順序は run → process → append_raw/materialize → state.complete。
raw commit → SQLiteの現在値stage → Iceberg再構築 → SQLite完了位置
→ Kafkaの同期commit、の順を守る。確定点ごとの保存物は独立しており、
Kafka・SQLite・全Iceberg表を一つのtransactionで更新する機構ではない。
source SQLへのDMLは実行しない。再開時は対応するmapping・state volume・
Kafka履歴を保持し、失ったstateを空のまま継続しない。
"""

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from config import CONTRACTS, mappings, settings, topic_names
from state import State, assignment_offset

RAW_COLUMNS = [
    "event_id",
    "topic",
    "partition",
    "offset",
    "key_json",
    "envelope_json",
    "ingested_at",
]


def raw_rows(records):
    """Kafka位置・key・envelopeを、raw表用の文字列行へ変換して返す。

    topic/partition/offsetをevent IDにし、同じ配送位置を識別する。
    tombstoneもkeyと空envelopeで残す。DELETE本体との混同や破棄をしない。
    この変換自体はDB・Icebergへ書き込まない。
    """
    return [
        {
            "event_id": json.dumps(
                [record["topic"], record["partition"], record["offset"]],
                separators=(",", ":"),
            ),
            "topic": record["topic"],
            "partition": str(record["partition"]),
            "offset": str(record["offset"]),
            "key_json": record["key"] or "",
            "envelope_json": record["value"] or "",
            "ingested_at": record["ingested_at"],
        }
        for record in records
    ]


def append_raw(catalog, records):
    """イベントの業務解釈より前に、未保存の配送位置をrawへcommitする。

    既存表と今回batchの両方でevent IDを重複除去し、追記件数を返す。
    後段でenvelopeを拒否しても原本は残り得る。別state dirの並列writerまで
    防ぐtransactionではないため、起動側の単一writer運用と組み合わせる。
    """
    import pyarrow as pa
    from pyiceberg.expressions import In

    if not catalog.namespace_exists("cdc_events"):
        catalog.create_namespace("cdc_events")
    identifier = "cdc_events.events"
    if not catalog.table_exists(identifier):
        catalog.create_table(
            identifier,
            schema=pa.schema([(name, pa.string()) for name in RAW_COLUMNS]),
            properties={"format-version": "2"},
        )
    table = catalog.load_table(identifier)
    rows = raw_rows(records)
    # 再配送と同batch内の重複を区別せず、配送位置で一度だけrawへ追記する。
    existing = (
        table.scan(
            row_filter=In("event_id", [row["event_id"] for row in rows]),
            selected_fields=("event_id",),
        )
        .to_arrow()
        .to_pylist()
    )
    seen = {row["event_id"] for row in existing}
    pending = []
    for row in rows:
        if row["event_id"] not in seen:
            pending.append(row)
            seen.add(row["event_id"])
    if pending:
        table.append(
            pa.Table.from_pylist(pending, schema=table.schema().as_arrow())
        )
    return len(pending)


def materialize(admin, state):
    """SQLite現在値からcdc_bronzeの6表を置換し、CDC派生SQLを実行する。

    戻り値はメダリオンの完了文数。各表は独立してcommitされるため、途中失敗で
    表ごとに更新世代が異なる場合がある。次回の全再構築で整える方式であり、
    全表rollbackや増分MERGEを行う処理ではない。
    """
    import pyarrow as pa

    admin.ensure_tables("cdc_")
    catalog = admin.catalog()
    for contract in CONTRACTS:
        target = contract["table"]
        table = catalog.load_table("cdc_bronze." + target)
        arrow = pa.Table.from_pylist(
            state.rows(target), schema=table.schema().as_arrow()
        )
        table.overwrite(arrow)
    return admin.medallion("cdc_")


def process(admin, state, records, mapping, topics):
    """1batchをraw→現在値stage→派生表→local完了位置の順に反映する。

    完了済みoffsetは再適用せず、state内のLSN判定で改訂・削除の順序を守る。
    派生表が成功した後だけcheckpointを進める。Kafka commitはrunが後で行う。
    Trinoの取消未確定はstateにもblockedを保存し、再起動で見失わない。
    """
    # 工程1: 原本を先に確定する。後段の検証失敗でも調査用rawは残り得る。
    count = append_raw(admin.catalog(), records)
    contracts = {item["table"]: item["columns"] for item in CONTRACTS}
    for record in records:
        target = topics.get(record["topic"])
        if target is None:
            raise ValueError("Unexpected Kafka topic; no offset advanced.")
        if record["offset"] <= state.offset(
            record["topic"], record["partition"]
        ):
            continue
        if record["value"] is not None:
            source = json.loads(record["value"]).get("source", {})
            if source.get("db") != os.environ["CDC_SQLSERVER_DATABASE"]:
                raise ValueError("CDC event database does not match settings.")
        # 工程2: LSN/削除版を現在値へ反映。完了offsetはまだ進めない。
        state.stage(record, target, mapping[target], contracts[target])
    try:
        # 工程3: 現在値6表と派生表を再構築する。1表ずつのcommitになる。
        result = materialize(admin, state)
    except Exception:
        if admin.STATE.get("blocked"):
            state.set("blocked", "true")
        raise
    # 工程4: 派生まで成功した後だけlocal完了位置を永続化する。
    state.complete(records)
    return {"raw_events_appended": count, **result}


def refuse_lost_state(admin, state):
    """local state不在なのにraw/currentがある場合、整合不明の再開を拒否する。

    初回の空環境だけinitializedを記録する。Kafka側の配送位置だけを使い、
    過去の現在値を失った状態で最新へ飛ぶことを防ぐ。自動復元は行わない。
    """
    if state.get("initialized"):
        return
    catalog = admin.catalog()
    identifiers = ["cdc_events.events"] + [
        "cdc_bronze." + item["table"] for item in CONTRACTS
    ]
    for identifier in identifiers:
        if catalog.table_exists(identifier):
            table = catalog.load_table(identifier)
            if table.scan(limit=1).to_arrow().num_rows:
                raise ValueError(
                    "Restore the CDC state volume before resuming."
                )
    state.set("initialized", "true")


def run(directory, mapping_file):
    """state dirを排他し、有限poll窓のbatchを失敗・中断まで繰り返す。

    source identityとmappingをstateへ結び付け、Kafkaの自動commitを無効にする。
    16MiB超のvalueはdecode前に拒否し、派生表とlocal checkpoint成功後にだけ
    brokerへ次offsetを同期commitする。失敗は上位へ返し、無限再試行で隠さない。
    同じstate dirのlockは、別volumeや外部writerの競合までは調停しない。
    """
    import admin
    from confluent_kafka import Consumer, KafkaError, TopicPartition

    values = settings()
    mapping = mappings(mapping_file)
    topics = topic_names(values, mapping)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (directory / "consumer.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # 同じstateを別DB/mapへ流用しない。identityの一致検査はStateが担う。
    identity = {
        "database": values["CDC_SQLSERVER_DATABASE"],
        "host": values["CDC_SQLSERVER_HOST"],
        "port": values["CDC_SQLSERVER_PORT"],
        "prefix": values["CDC_TOPIC_PREFIX"],
        "mapping": mapping,
    }
    state = State(directory / "state.sqlite3", identity)
    if state.get("blocked") == "true":
        raise RuntimeError("CDC is blocked pending query cancellation review.")
    refuse_lost_state(admin, state)
    consumer = Consumer(
        {
            "bootstrap.servers": "cdc-kafka:9092",
            "group.id": values["CDC_GROUP_ID"],
            # pollしただけでは完了扱いにせず、下段の同期commitだけで進める。
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "error",
            "allow.auto.create.topics": False,
            "max.poll.interval.ms": 3600000,
        }
    )

    def assign(client, partitions):
        """partition割当時に、localの完了位置とKafka保持範囲から再開位置を決める。

        未処理範囲が保持切れなら拒否し、latestへ暗黙に進めない。
        """
        for partition in partitions:
            offset = state.offset(partition.topic, partition.partition)
            low, high = client.get_watermark_offsets(partition, timeout=10)
            partition.offset = assignment_offset(offset, low, high)
        client.assign(partitions)

    consumer.subscribe(list(topics), on_assign=assign)
    print(json.dumps({"ready": True, "namespaces": "cdc_*"}), flush=True)
    try:
        while True:
            records = []
            # 窓は有限だがconsumer自体は常駐する。空pollで正常終了しない。
            deadline = time.monotonic() + int(values["CDC_POLL_SECONDS"])
            while len(records) < int(values["CDC_BATCH_SIZE"]):
                message = consumer.poll(1)
                if message is not None:
                    if message.error():
                        if (
                            message.error().code()
                            == KafkaError.UNKNOWN_TOPIC_OR_PART
                        ):
                            continue
                        raise RuntimeError("Kafka returned a consumer error.")
                    payload = message.value()
                    key = message.key()
                    if len(payload or b"") > 16 * 1024 * 1024:
                        raise ValueError("CDC event exceeds the 16 MiB limit.")
                    records.append(
                        {
                            "topic": message.topic(),
                            "partition": message.partition(),
                            "offset": message.offset(),
                            "key": key.decode("utf-8") if key else None,
                            "value": payload.decode("utf-8")
                            if payload
                            else None,
                            "ingested_at": datetime.now(
                                timezone.utc
                            ).isoformat(),
                        }
                    )
                if time.monotonic() >= deadline:
                    break
            if not records:
                continue
            # 工程5: Icebergとlocal完了位置より先にKafkaをackしない。
            # broker応答だけ失敗しても、再開位置はlocal checkpointから決める。
            result = process(admin, state, records, mapping, topics)
            offsets = {}
            for record in records:
                key = (record["topic"], record["partition"])
                # Kafka commitへは、処理した位置の次offsetを渡す。
                offsets[key] = max(offsets.get(key, -1), record["offset"] + 1)
            consumer.commit(
                offsets=[
                    TopicPartition(topic, partition, offset)
                    for (topic, partition), offset in offsets.items()
                ],
                asynchronous=False,
            )
            print(
                json.dumps({"processed": len(records), **result}), flush=True
            )
    finally:
        consumer.close()
        lock.close()


def main():
    """公開可能なCLI入力を読み、失敗時は例外型だけを出して終了する。

    永続stateとprivate mappingのpathをrunへ渡す。vendor logやdriverの生例外は
    source名・接続先・資格情報を含み得るため、公開標準出力へ転送しない。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path("/state"),
        help=(
            "SQLite現在値・checkpoint・lockの永続dir。既定: /state。"
            "再開時も同じ内容を保持し、空のdirへ置き換えません"
        ),
    )
    parser.add_argument(
        "--table-map",
        default="/run/cdc/table-map.json",
        help="許可する6表・列の対応JSON。既定: /run/cdc/table-map.json",
    )
    args = parser.parse_args()
    # vendor logにsource名や接続情報が出るため、公開出力は自前の件数に絞る。
    logging.disable(logging.CRITICAL)
    try:
        run(args.state_dir, args.table_map)
    except Exception as error:
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    main()
