"""Retain raw CDC events and build isolated current-state Iceberg tables."""

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
    """Preserve complete Kafka keys and envelopes, including tombstone
    events.
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
    """Commit each Kafka position once before interpreting source row data."""
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
    """Replace six isolated current-state tables, then run existing SQL
    rules.
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
    """Order raw retention, replayable state, medallion, and local
    checkpoint.
    """
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
        state.stage(record, target, mapping[target], contracts[target])
    try:
        result = materialize(admin, state)
    except Exception:
        if admin.STATE.get("blocked"):
            state.set("blocked", "true")
        raise
    state.complete(records)
    return {"raw_events_appended": count, **result}


def refuse_lost_state(admin, state):
    """Do not resume committed Kafka offsets after durable state was lost."""
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
    """Run one durable consumer, committing Kafka only after Iceberg
    success.
    """
    import admin
    from confluent_kafka import Consumer, KafkaError, TopicPartition

    values = settings()
    mapping = mappings(mapping_file)
    topics = topic_names(values, mapping)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (directory / "consumer.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            "enable.auto.commit": False,
            "enable.auto.offset.store": False,
            "auto.offset.reset": "error",
            "allow.auto.create.topics": False,
            "max.poll.interval.ms": 3600000,
        }
    )

    def assign(client, partitions):
        """Use only the durable completed checkpoint when partitions return."""
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
            result = process(admin, state, records, mapping, topics)
            offsets = {}
            for record in records:
                key = (record["topic"], record["partition"])
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
    """Keep errors free of SQL source values and credential-bearing URLs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("/state"))
    parser.add_argument("--table-map", default="/run/cdc/table-map.json")
    args = parser.parse_args()
    # Vendor HTTP/Kafka logs can contain source names; only our counters print.
    logging.disable(logging.CRITICAL)
    try:
        run(args.state_dir, args.table_map)
    except Exception as error:
        print(json.dumps({"failed": True, "error_type": type(error).__name__}))
        sys.exit(1)


if __name__ == "__main__":
    main()
