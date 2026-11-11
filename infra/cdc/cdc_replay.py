"""Rebuild fixed CDC state from a bounded, pinned immutable raw snapshot.

This operation never reads SQL Server or changes a connector. It requires a
complete contiguous Kafka delivery history from offset zero for all six
tables. An empty table with no event is indistinguishable from missing raw
history, so this version refuses that case instead of inventing a baseline.

Derived tables commit separately. A marker on the old SQLite state is durable
before materialization starts; an interrupted generation cannot be resumed as
healthy. Valid local state publication precedes the caller's Kafka ack.
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from event_limits import MAX_RECORD_BYTES
from state import State

MAX_EVENTS = 100000
MAX_BYTES = 128 * 1024 * 1024
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


def flush_sqlite(store):
    """Close a durable standalone SQLite file before an atomic replacement."""
    store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.connection.execute("PRAGMA journal_mode=DELETE")
    store.connection.close()
    descriptor = os.open(store.path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_diagnostics(original, candidate):
    """Preserve bounded durable error/rebuild history in the new generation."""
    for table, fields in (
        (
            "processing_errors",
            "topic,partition_id,offset_id,error_code,recorded_at",
        ),
        (
            "rebuild_history",
            "request_id,status,raw_snapshot_id,error_code,updated_at",
        ),
    ):
        # Names/columns are fixed internal SQL, never supplied by a request.
        cursor = original.connection.execute(
            "SELECT " + fields + " FROM " + table
        )
        copied = 0
        with candidate.connection:
            while True:
                rows = cursor.fetchmany(128)
                if not rows:
                    break
                copied += len(rows)
                if copied > MAX_EVENTS:
                    raise RebuildError("CDC_DIAGNOSTIC_HISTORY_LIMIT")
                candidate.connection.executemany(
                    "INSERT OR REPLACE INTO " + table + " VALUES (?,?,?,?,?)",
                    rows,
                )


def rebuild(
    admin,
    original,
    mapping,
    topics,
    request_id,
    *,
    materialize,
    validate_resume=None,
    maximum=MAX_EVENTS,
    byte_limit=MAX_BYTES,
):
    """Validate raw, materialize, then publish one complete local generation.

    The caller holds the consumer directory lock and pauses delivery. SQL
    cancellation blockers are never cleared here. If validation fails, current
    rows and completed offsets stay intact. Once materialization starts, the
    old pending marker remains on failure because derived tables may differ.
    """
    if not original.fixed_source or not re.fullmatch(
        r"[0-9a-f]{32}", request_id
    ):
        raise RebuildError("CDC_REBUILD_NOT_SUPPORTED")
    if original.get("blocked") == "true" or admin.STATE.get("blocked"):
        raise RebuildError("CDC_QUERY_REVIEW_REQUIRED")
    catalog = admin.catalog()
    if not catalog.table_exists("sqlcdc_events.events"):
        raise RebuildError("CDC_RAW_HISTORY_MISSING")
    table = catalog.load_table("sqlcdc_events.events")
    if table.properties.get("lakehouse.fixed-cdc.identity") != (
        original.get("identity")
    ):
        raise RebuildError("CDC_RAW_SOURCE_MISMATCH")
    snapshot = table.current_snapshot()
    if snapshot is None or type(snapshot.snapshot_id) is not int:
        raise RebuildError("CDC_RAW_HISTORY_MISSING")
    snapshot_id = snapshot.snapshot_id
    headers = raw_headers(
        table,
        snapshot_id,
        topics,
        original.completed_offsets(),
        maximum=maximum,
    )
    directory = Path(original.path).parent
    descriptor, name = tempfile.mkstemp(
        prefix=".rebuild-", suffix=".sqlite3", dir=directory
    )
    os.close(descriptor)
    temporary_path = Path(name)
    temporary = None
    published = False
    try:
        temporary = State(temporary_path, original.identity)
        total_bytes = 0
        contracts = {item["table"]: item["columns"] for item in admin.CONTRACT}
        for header in headers:
            record, size = read_record(table, snapshot_id, header)
            total_bytes += size
            if total_bytes > byte_limit:
                raise RebuildError("CDC_RAW_REBUILD_BYTE_LIMIT")
            target = topics[record["topic"]]
            temporary.stage(record, target, mapping[target], contracts[target])
        # The hidden candidate can hold checkpoints without acknowledging
        # delivery. It becomes active only after all derived tables succeed.
        temporary.complete(headers)
        temporary.set("initialized", "true")
        copy_diagnostics(original, temporary)
        if validate_resume is not None:
            validate_resume(temporary)
        original.record_rebuild(request_id, "running", snapshot_id)
        original.set(
            "rebuild_pending",
            json.dumps(
                {
                    "request_id": request_id,
                    "raw_snapshot_id": snapshot_id,
                }
            ),
        )
        materialize(admin, temporary)
        temporary.record_rebuild(request_id, "succeeded", snapshot_id)
        temporary.set("rebuild_pending", "")
        temporary.set("rebuild_generation", request_id)
        flush_sqlite(temporary)
        temporary = None
        # Closing the old connection eliminates live WAL sidecars before
        # replacement. The active file's pending marker survives any crash
        # before os.replace, so startup cannot trust a mixed derived state.
        flush_sqlite(original)
        os.replace(temporary_path, original.path)
        published = True
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        active = State(original.path, original.identity)
        return active, snapshot_id
    except Exception:
        if temporary is not None:
            temporary.connection.close()
        # The caller reopens the active path if a filesystem error happened
        # after close/publication; a valid published checkpoint is retained.
        if admin.STATE.get("blocked") and not published:
            try:
                original.set("blocked", "true")
            except Exception:
                pass
        raise
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(temporary_path) + suffix).unlink(missing_ok=True)
