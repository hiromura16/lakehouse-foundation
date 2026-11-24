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
from pathlib import Path

from raw_history import (
    MAX_EVENTS,
    open_raw_snapshot,
    raw_headers,
    read_record,
)
from raw_history import (
    RebuildError as RebuildError,
)
from state import State

MAX_BYTES = 128 * 1024 * 1024


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
    table, snapshot_id = open_raw_snapshot(
        admin.catalog(), lambda: original.get("identity")
    )
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
