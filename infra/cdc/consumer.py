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
from hashlib import sha256
from pathlib import Path

from config import CONTRACTS, selected_mapping, settings, topic_names
from fixed_source import preflight
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
FIXED_RAW_COLUMNS = RAW_COLUMNS + [
    "operation",
    "tombstone",
    "before_json",
    "after_json",
    "commit_lsn",
    "change_lsn",
    "event_serial_no",
    "transaction_id",
    "source_ts_ms",
    "run_id",
]


def raw_rows(records, *, fixed=False):
    """Kafka位置・key・envelopeを、raw表用の文字列行へ変換して返す。

    topic/partition/offsetをevent IDにし、同じ配送位置を識別する。
    fixedではnull envelopeと空文字列を別々に保持し、DELETE本体とも区別する。
    genericの既存raw schemaと空envelopeの表現は互換性のため維持する。
    この変換自体はDB・Icebergへ書き込まない。
    """
    rows = [
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
    if fixed:
        for row, record in zip(rows, records, strict=True):
            row["key_json"] = record["key"]
            row["envelope_json"] = record["value"]
            # Malformed envelopes are still retained. Interpretation failure
            # must not destroy evidence or acknowledge offsets.
            try:
                envelope = json.loads(record["value"] or "null")
            except (TypeError, ValueError):
                envelope = None
            envelope = envelope if isinstance(envelope, dict) else {}
            source = envelope.get("source") or {}
            source = source if isinstance(source, dict) else {}
            transaction = envelope.get("transaction") or {}
            transaction = transaction if isinstance(transaction, dict) else {}
            current = envelope.get("after") or envelope.get("before") or {}
            current = current if isinstance(current, dict) else {}
            tombstone = record["value"] is None
            operation = "tombstone" if tombstone else envelope.get("op")
            row.update(
                operation=operation if isinstance(operation, str) else None,
                tombstone="true" if tombstone else "false",
                before_json=json.dumps(
                    envelope.get("before"), ensure_ascii=False
                ),
                after_json=json.dumps(
                    envelope.get("after"), ensure_ascii=False
                ),
                commit_lsn=source.get("commit_lsn"),
                change_lsn=source.get("change_lsn"),
                event_serial_no=(
                    str(source["event_serial_no"])
                    if "event_serial_no" in source
                    else None
                ),
                transaction_id=transaction.get("id"),
                source_ts_ms=(
                    str(source["ts_ms"])
                    if source.get("ts_ms") is not None
                    else None
                ),
                run_id=current.get("run_id"),
            )
            # Convenience columns supplement the untouched envelope_json.
            for name in FIXED_RAW_COLUMNS:
                if row.get(name) is not None and not isinstance(
                    row[name], str
                ):
                    row[name] = json.dumps(row[name], ensure_ascii=False)
    return rows


def message_record(message):
    """Decode one bounded Kafka message without collapsing empty bytes.

    A null value is a Kafka tombstone. Empty bytes are an invalid JSON value
    whose original representation must remain distinguishable in fixed raw.
    """
    payload = message.value()
    key = message.key()
    if len(payload if payload is not None else b"") > 16 * 1024 * 1024:
        raise ValueError("CDC event exceeds the 16 MiB limit.")
    return {
        "topic": message.topic(),
        "partition": message.partition(),
        "offset": message.offset(),
        "key": key.decode("utf-8") if key is not None else None,
        "value": payload.decode("utf-8") if payload is not None else None,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


def append_raw(catalog, records, *, namespace="cdc_events", identity=None):
    """イベントの業務解釈より前に、未保存の配送位置をrawへcommitする。

    既存表と今回batchの両方でevent IDを重複除去し、追記件数を返す。
    後段でenvelopeを拒否しても原本は残り得る。別state dirの並列writerまで
    防ぐtransactionではないため、起動側の単一writer運用と組み合わせる。
    """
    import pyarrow as pa
    from pyiceberg.expressions import In

    if not records:
        return 0
    if namespace not in {"cdc_events", "sqlcdc_events"}:
        raise ValueError("Unsupported CDC raw namespace.")
    fixed = namespace == "sqlcdc_events"
    if fixed and (not isinstance(identity, str) or len(identity) != 64):
        raise ValueError("Fixed CDC raw history needs its source identity.")
    columns = FIXED_RAW_COLUMNS if fixed else RAW_COLUMNS
    if not catalog.namespace_exists(namespace):
        catalog.create_namespace(namespace)
    identifier = namespace + ".events"
    properties = {"format-version": "2"}
    if fixed:
        properties["lakehouse.fixed-cdc.identity"] = identity
    if not catalog.table_exists(identifier):
        catalog.create_table(
            identifier,
            schema=pa.schema([(name, pa.string()) for name in columns]),
            properties=properties,
        )
    table = catalog.load_table(identifier)
    if fixed and (
        table.properties.get("lakehouse.fixed-cdc.identity") != identity
        or table.schema().as_arrow().names != columns
    ):
        raise ValueError("Fixed CDC raw table ownership or schema differs.")
    rows = raw_rows(records, fixed=fixed)
    # 再配送と同batch内の重複を区別せず、配送位置で一度だけrawへ追記する。
    existing = (
        table.scan(
            row_filter=In("event_id", [row["event_id"] for row in rows]),
            selected_fields=("event_id", "key_json", "envelope_json")
            if fixed
            else ("event_id",),
        )
        .to_arrow()
        .to_pylist()
    )
    seen = {row["event_id"]: row for row in existing}
    pending = []
    for row in rows:
        previous = seen.get(row["event_id"])
        if (
            fixed
            and previous is not None
            and any(
                previous[column] != row[column]
                for column in ("key_json", "envelope_json")
            )
        ):
            raise ValueError("A fixed CDC delivery position changed payload.")
        if previous is None:
            pending.append(row)
            seen[row["event_id"]] = row
    if pending:
        table.append(
            pa.Table.from_pylist(pending, schema=table.schema().as_arrow())
        )
    return len(pending)


def materialize(admin, state, *, prefix="cdc_"):
    """SQLite現在値からcdc_bronzeの6表を置換し、CDC派生SQLを実行する。

    戻り値はメダリオンの完了文数。各表は独立してcommitされるため、途中失敗で
    表ごとに更新世代が異なる場合がある。次回の全再構築で整える方式であり、
    全表rollbackや増分MERGEを行う処理ではない。
    """
    import pyarrow as pa

    if prefix not in {"cdc_", "sqlcdc_"}:
        raise ValueError("Unsupported CDC materialization prefix.")
    admin.ensure_tables(prefix)
    catalog = admin.catalog()
    for contract in CONTRACTS:
        target = contract["table"]
        table = catalog.load_table(prefix + "bronze." + target)
        arrow = pa.Table.from_pylist(
            state.rows(target), schema=table.schema().as_arrow()
        )
        table.overwrite(arrow)
    return admin.medallion(prefix)


def process(admin, state, records, mapping, topics, *, values=None):
    """1batchをraw→現在値stage→派生表→local完了位置の順に反映する。

    完了済みoffsetは再適用せず、state内のLSN判定で改訂・削除の順序を守る。
    派生表が成功した後だけcheckpointを進める。Kafka commitはrunが後で行う。
    Trinoの取消未確定はstateにもblockedを保存し、再起動で見失わない。
    """
    # 工程1: 原本を先に確定する。後段の検証失敗でも調査用rawは残り得る。
    fixed = values is not None and values.get("CDC_SOURCE_MODE") == (
        "fixed-demo"
    )
    if fixed:
        count = append_raw(
            admin.catalog(),
            records,
            namespace="sqlcdc_events",
            identity=identity_digest(source_identity(values, mapping)),
        )
        # Phase 4 retains raw events safely. Fixed current-state decoding is
        # introduced with the separately tested replay/materialization phase.
        if not getattr(state, "fixed_source", False):
            raise ValueError("Fixed CDC current-state support is not ready.")
    else:
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
            database = (
                values["CDC_SQLSERVER_DATABASE"]
                if values is not None
                else os.environ["CDC_SQLSERVER_DATABASE"]
            )
            if source.get("db") != database:
                raise ValueError("CDC event database does not match settings.")
        # 工程2: LSN/削除版を現在値へ反映。完了offsetはまだ進めない。
        state.stage(record, target, mapping[target], contracts[target])
    try:
        # 工程3: 現在値6表と派生表を再構築する。1表ずつのcommitになる。
        result = (
            materialize(admin, state, prefix="sqlcdc_")
            if fixed
            else materialize(admin, state)
        )
    except Exception:
        if admin.STATE.get("blocked"):
            state.set("blocked", "true")
        raise
    # 工程4: 派生まで成功した後だけlocal完了位置を永続化する。
    state.complete(records)
    return {"raw_events_appended": count, **result}


def refuse_lost_state(admin, state, *, fixed=False):
    """local state不在なのにraw/currentがある場合、整合不明の再開を拒否する。

    初回の空環境だけinitializedを記録する。Kafka側の配送位置だけを使い、
    過去の現在値を失った状態で最新へ飛ぶことを防ぐ。自動復元は行わない。
    """
    if state.get("initialized"):
        return
    catalog = admin.catalog()
    raw = "sqlcdc_events.events" if fixed else "cdc_events.events"
    prefix = "sqlcdc_" if fixed else "cdc_"
    identifiers = [raw] + [
        prefix + "bronze." + item["table"] for item in CONTRACTS
    ]
    for identifier in identifiers:
        if catalog.table_exists(identifier):
            table = catalog.load_table(identifier)
            if table.scan(limit=1).to_arrow().num_rows:
                raise ValueError(
                    "Restore the CDC state volume before resuming."
                )
    state.set("initialized", "true")


def source_identity(values, mapping):
    """Keep generic identities and bind fixed state to its installation."""
    identity = {
        "database": values["CDC_SQLSERVER_DATABASE"],
        "host": values["CDC_SQLSERVER_HOST"],
        "port": values["CDC_SQLSERVER_PORT"],
        "prefix": values["CDC_TOPIC_PREFIX"],
        "mapping": mapping,
    }
    if values.get("CDC_SOURCE_MODE") == "fixed-demo":
        identity.update(
            source_mode="fixed-demo",
            schema_version=1,
            installation_id=values["DEMO_SQLSERVER_INSTALLATION_ID"],
        )
    return identity


def identity_digest(identity):
    """Use the same canonical digest as SQLite's existing identity guard."""
    return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


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
    mapping = selected_mapping(values, mapping_file)
    topics = topic_names(values, mapping)
    fixed = values.get("CDC_SOURCE_MODE") == "fixed-demo"
    if fixed:
        preflight(values, mapping)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (directory / "consumer.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # 同じstateを別DB/mapへ流用しない。identityの一致検査はStateが担う。
    identity = source_identity(values, mapping)
    state = State(directory / "state.sqlite3", identity)
    if state.get("blocked") == "true":
        raise RuntimeError("CDC is blocked pending query cancellation review.")
    refuse_lost_state(admin, state, fixed=fixed)
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
                    records.append(message_record(message))
                if time.monotonic() >= deadline:
                    break
            if not records:
                continue
            # 工程5: Icebergとlocal完了位置より先にKafkaをackしない。
            # broker応答だけ失敗しても、再開位置はlocal checkpointから決める。
            result = process(
                admin, state, records, mapping, topics, values=values
            )
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
