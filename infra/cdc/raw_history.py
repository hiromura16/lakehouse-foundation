"""固定 CDC の Raw 履歴を、読み取り専用で検証する。

カタログから対象の履歴と snapshot を固定し、行の順序・完全性・
payload を検証する。派生表や SQLite 状態、配送確認を変更しない。
"""

import json
import re
from datetime import datetime

from event_limits import MAX_RECORD_BYTES

MAX_EVENTS = 100000
HEADER_COLUMNS = ("event_id", "topic", "partition", "offset")
BODY_COLUMNS = (
    *HEADER_COLUMNS,
    "key_json",
    "envelope_json",
    "ingested_at",
    "tombstone",
)


class RebuildError(ValueError):
    """Retain only a fixed diagnostic code; source payloads stay private."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def open_raw_snapshot(catalog, read_identity):
    """表が存在するときだけローカルの識別値を読み、snapshot を選ぶ。"""
    if not catalog.table_exists("sqlcdc_events.events"):
        raise RebuildError("CDC_RAW_HISTORY_MISSING")
    table = catalog.load_table("sqlcdc_events.events")
    if table.properties.get("lakehouse.fixed-cdc.identity") != read_identity():
        raise RebuildError("CDC_RAW_SOURCE_MISMATCH")
    snapshot = table.current_snapshot()
    if snapshot is None or type(snapshot.snapshot_id) is not int:
        raise RebuildError("CDC_RAW_HISTORY_MISSING")
    return table, snapshot.snapshot_id


def raw_headers(table, snapshot_id, topics, completed, *, maximum=MAX_EVENTS):
    """Validate bounded identities and coverage before changing any state."""
    rows = (
        table.scan(
            snapshot_id=snapshot_id,
            selected_fields=HEADER_COLUMNS,
            limit=maximum + 1,
        )
        .to_arrow()
        .to_pylist()
    )
    if not rows or len(rows) > maximum:
        raise RebuildError("CDC_RAW_HISTORY_LIMIT_OR_EMPTY")
    grouped = {}
    observed_tables = set()
    origins = {}
    headers = []
    for row in rows:
        topic = row.get("topic")
        if topic not in topics:
            raise RebuildError("CDC_RAW_SOURCE_MISMATCH")
        numbers = []
        for name in ("partition", "offset"):
            value = row.get(name)
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9]{1,19}", value
            ):
                raise RebuildError("CDC_RAW_POSITION_INVALID")
            numbers.append(int(value))
        partition, offset = numbers
        if partition != 0 or offset >= 2**63:
            raise RebuildError("CDC_RAW_POSITION_INVALID")
        identity = json.dumps(
            [topic, partition, offset], separators=(",", ":")
        )
        if row.get("event_id") != identity:
            raise RebuildError("CDC_RAW_POSITION_INVALID")
        group = (topic, partition)
        grouped.setdefault(group, []).append(offset)
        target = topics[topic]
        prior = origins.setdefault(target, group)
        if prior != group:
            raise RebuildError("CDC_RAW_SOURCE_MISMATCH")
        observed_tables.add(target)
        headers.append(
            {
                "event_id": identity,
                "topic": topic,
                "partition": partition,
                "offset": offset,
            }
        )
    if observed_tables != set(topics.values()):
        raise RebuildError("CDC_RAW_BASELINE_INCOMPLETE")
    for group, offsets in grouped.items():
        ordered = sorted(offsets)
        if ordered != list(range(len(ordered))):
            raise RebuildError("CDC_RAW_HISTORY_GAP")
        if completed.get(group, -1) > ordered[-1]:
            raise RebuildError("CDC_RAW_HISTORY_INCOMPLETE")
    if any(group not in grouped for group in completed):
        raise RebuildError("CDC_RAW_HISTORY_INCOMPLETE")
    return sorted(
        headers,
        key=lambda row: (row["topic"], row["partition"], row["offset"]),
    )


def read_record(table, snapshot_id, header):
    """Fetch one pinned payload at a time and preserve NULL versus empty."""
    from pyiceberg.expressions import EqualTo

    rows = (
        table.scan(
            snapshot_id=snapshot_id,
            row_filter=EqualTo("event_id", header["event_id"]),
            selected_fields=BODY_COLUMNS,
            limit=2,
        )
        .to_arrow()
        .to_pylist()
    )
    if len(rows) != 1:
        raise RebuildError("CDC_RAW_POSITION_INVALID")
    row = rows[0]
    if any(str(row.get(name)) != str(header[name]) for name in HEADER_COLUMNS):
        raise RebuildError("CDC_RAW_POSITION_INVALID")
    payload, key = row.get("envelope_json"), row.get("key_json")
    if any(
        value is not None and not isinstance(value, str)
        for value in (payload, key)
    ):
        raise RebuildError("CDC_RAW_PAYLOAD_INVALID")
    if row.get("tombstone") != ("true" if payload is None else "false"):
        raise RebuildError("CDC_RAW_PAYLOAD_INVALID")
    size = len((payload or "").encode()) + len((key or "").encode())
    if size > MAX_RECORD_BYTES:
        raise RebuildError("CDC_RAW_MESSAGE_LIMIT")
    stamp = row.get("ingested_at")
    try:
        if not isinstance(stamp, str) or len(stamp) > 64:
            raise ValueError
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        raise RebuildError("CDC_RAW_PAYLOAD_INVALID") from None
    return {
        "topic": header["topic"],
        "partition": header["partition"],
        "offset": header["offset"],
        "key": key,
        "value": payload,
        "ingested_at": stamp,
    }, size
