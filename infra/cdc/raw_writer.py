"""CDC 配送記録だけを Raw 履歴へ保存する。

業務表への反映、ローカル完了位置、Kafka 確認は管理しない。
複数 writer の排他条件は呼出側が保証する。
"""

import json

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
