"""Keep durable CDC row versions and completed Kafka offsets in SQLite."""

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256

TIMESTAMP_COLUMNS = {
    "updated_at",
    "event_time",
    "shipped_at",
    "start_time",
    "end_time",
}
DATE_COLUMNS = {"planned_date", "order_date", "promised_date"}
MAX_CURRENT_BYTES = 64 * 1024 * 1024
MAX_CURRENT_ROWS = 100000


def nano_timestamp(value):
    """Preserve datetime2(7) nanoseconds without floating point conversion."""
    if type(value) is not int or not -(2**63) <= value < 2**63 or value % 100:
        raise ValueError("Fixed CDC requires exact datetime2(7) nanoseconds.")
    seconds, nanos = divmod(value, 1000000000)
    stamp = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=seconds
    )
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos // 100:07d}Z"


def fixed_business_row(current, columns, primary_key, key):
    """Reuse the batch reader's bounded, NULL-preserving sidecar contract."""
    import importlib
    import sys

    from fixed_source import ROOT

    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    batch = importlib.import_module("sqlserver_batch")
    return batch.original_values(
        current.get("original_values"), columns, primary_key, key
    )


def assignment_offset(completed, low, high):
    """Reject lost history or a reset broker before assigning a partition."""
    next_offset = completed + 1
    if next_offset < low:
        raise RuntimeError("Kafka retention passed the durable checkpoint.")
    if next_offset > high:
        raise RuntimeError("Kafka offsets were reset; restore the source.")
    return next_offset


def lsn(value):
    """Parse the three fixed-width SQL Server log sequence components."""
    import re

    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}:[0-9a-fA-F]{8}:[0-9a-fA-F]{4}", value
    ):
        raise ValueError("A streaming event needs a valid SQL Server LSN.")
    return [int(part, 16) for part in value.split(":")]


def version(envelope, offset):
    """Order streaming changes after snapshots, then by LSN and event order."""
    if envelope["op"] == "r":
        return [0, 0, 0, 0, 0, 0, 0, offset]
    source = envelope["source"]
    serial = source.get("event_serial_no", 0)
    if not str(serial).isdigit():
        raise ValueError("Invalid SQL Server event serial number.")
    return (
        [1]
        + lsn(source.get("commit_lsn"))
        + lsn(source.get("change_lsn"))
        + [int(serial)]
    )


def text_value(value, column):
    """Convert known Debezium logical dates and timestamps to UTC strings."""
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool)):
        raise ValueError("Unsupported source field type for the contract.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Nonfinite source values are unsupported.")
    if isinstance(value, (int, float)) and column in TIMESTAMP_COLUMNS:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    if isinstance(value, int) and column in DATE_COLUMNS:
        return (
            (datetime(1970, 1, 1) + timedelta(days=value)).date().isoformat()
        )
    return str(value)


class State:
    """Persist current rows separately from successfully materialized
    offsets.
    """

    def __init__(self, path, identity):
        """Refuse to reuse a durable volume for a different SQL source."""
        self.path = path
        self.identity = dict(identity)
        self.fixed_source = identity.get("source_mode") == "fixed-demo"
        expected = sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        self.connection = sqlite3.connect(path)
        existing = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if self.fixed_source:
            self.check_existing_identity(existing, expected)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS current_rows (
                table_name TEXT NOT NULL, row_key TEXT NOT NULL,
                version TEXT NOT NULL, row_json TEXT,
                source_hash TEXT NOT NULL, ingested_at TEXT NOT NULL,
                PRIMARY KEY (table_name, row_key)
            );
            CREATE TABLE IF NOT EXISTS offsets (
                topic TEXT NOT NULL, partition_id INTEGER NOT NULL,
                offset_id INTEGER NOT NULL,
                PRIMARY KEY (topic, partition_id)
            );
            """
        )
        prior = self.get("identity")
        if prior and prior != expected:
            self.connection.close()
            raise ValueError("CDC state volume belongs to a different source.")
        self.set("identity", expected)
        if self.fixed_source:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS processing_errors (
                    topic TEXT NOT NULL, partition_id INTEGER NOT NULL,
                    offset_id INTEGER NOT NULL, error_code TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (topic, partition_id, offset_id)
                );
                CREATE TABLE IF NOT EXISTS rebuild_history (
                    request_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    raw_snapshot_id INTEGER, error_code TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def check_existing_identity(self, existing, expected):
        """Inspect a fixed state before any persistent PRAGMA or table DDL."""
        allowed = {
            "metadata",
            "current_rows",
            "offsets",
            "processing_errors",
            "rebuild_history",
        }
        try:
            if existing - allowed or (existing and "metadata" not in existing):
                raise ValueError
            prior = self.get("identity") if "metadata" in existing else None
            if prior and prior != expected:
                raise ValueError
            if not prior and any(
                # table names come only from the fixed allowlist above.
                self.connection.execute(
                    "SELECT 1 FROM " + table + " LIMIT 1"
                ).fetchone()
                for table in existing
            ):
                raise ValueError
        except Exception:
            self.connection.close()
            raise ValueError(
                "Restore the fixed CDC source identity and schema first."
            ) from None

    def get(self, key):
        """Read one internal metadata field without exposing source data."""
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        """Durably set an internal metadata field."""
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, value)
            )

    def offset(self, topic, partition):
        """Return the last offset whose Iceberg and medallion writes
        completed.
        """
        row = self.connection.execute(
            "SELECT offset_id FROM offsets WHERE topic = ? "
            "AND partition_id = ?",
            (topic, partition),
        ).fetchone()
        return row[0] if row else -1

    def stage(self, record, target, mapping, columns):
        """Apply an event to replayable state, retaining DELETE version
        markers.
        """
        if record["value"] is None:
            return
        envelope = json.loads(record["value"])
        if not isinstance(envelope, dict):
            raise ValueError("CDC expects a JSON event envelope.")
        if envelope.get("op") not in {"r", "c", "u", "d"}:
            raise ValueError("Unsupported CDC operation; raw event was kept.")
        source = envelope.get("source", {})
        if (
            source.get("schema") != mapping["source_schema"]
            or source.get("table") != mapping["source_table"]
            or (
                self.fixed_source
                and source.get("db") != self.identity["database"]
            )
        ):
            raise ValueError("CDC event origin does not match the mapping.")
        fields = mapping["columns"]
        primary_key = columns[0]
        key_column = next(key for key in fields if fields[key] == primary_key)
        key = json.loads(record["key"] or "{}")
        if (
            not isinstance(key, dict)
            or set(key) != {key_column}
            or (key[key_column] is None)
        ):
            raise ValueError("CDC requires a single declared business key.")
        if self.fixed_source and not isinstance(key[key_column], str):
            raise ValueError("Fixed CDC requires a string business key.")
        key_value = str(key[key_column])
        current = (
            envelope["before"] if envelope["op"] == "d" else envelope["after"]
        )
        if self.fixed_source:
            row = self.fixed_row(
                record, envelope, current, mapping, columns, key_value
            )
        elif not isinstance(current, dict) or any(
            key not in current for key in fields
        ):
            raise ValueError(
                "CDC source columns changed; review the contract."
            )
        else:
            row = {
                fields[key]: text_value(current[key], fields[key])
                for key in fields
            }
        if row[primary_key] != key_value or not key_value:
            raise ValueError("CDC key does not match its source row.")
        order = version(envelope, record["offset"])
        previous = self.connection.execute(
            "SELECT version FROM current_rows WHERE table_name = ? "
            "AND row_key = ?",
            (target, key_value),
        ).fetchone()
        if previous and json.loads(previous[0]) >= order:
            return
        payload = (
            None if envelope["op"] == "d" else json.dumps(row, sort_keys=True)
        )
        if self.fixed_source:
            self.check_budget(target, key_value, payload)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO current_rows "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    target,
                    key_value,
                    json.dumps(order),
                    payload,
                    sha256((record["value"] or "").encode()).hexdigest(),
                    record["ingested_at"],
                ),
            )
        count = self.connection.execute(
            "SELECT count(*) FROM current_rows WHERE table_name = ?", (target,)
        ).fetchone()[0]
        if count > 100000:
            raise ValueError("CDC demo current-state limit exceeded.")

    def fixed_row(self, record, envelope, current, mapping, columns, key):
        """Decode business originals; retain SQL watermark as provenance.

        SQL Server can omit an unchanged LOB from an UPDATE before image.
        INSERT/UPDATE use the after sidecar. DELETE needs only its key and LSN,
        not a before-image original_values LOB. Tombstones never reach here.
        """
        primary = columns[0]
        if not re.fullmatch(r"[!-~]{1,160}", key):
            raise ValueError("Fixed CDC needs a bounded stable business key.")
        if envelope["op"] == "d":
            if isinstance(current, dict) and current.get(primary, key) != key:
                raise ValueError("CDC DELETE key differs from its image.")
            return {primary: key}
        if not isinstance(current, dict) or current.get(primary) != key:
            raise ValueError("Fixed CDC after image differs from its key.")
        row = fixed_business_row(current, columns, primary, key)
        run_id = current.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(
            r"[0-9a-f]{12}", run_id
        ):
            raise ValueError("Fixed CDC after image has no tracked run ID.")
        delivery = json.dumps(
            [record["topic"], record["partition"], record["offset"]],
            separators=(",", ":"),
        )
        return {
            **row,
            "_source_system": "sqlserver-fixed-cdc",
            "_source_database": self.identity["database"],
            "_source_schema": mapping["source_schema"],
            "_source_table": mapping["source_table"],
            "_source_pk": key,
            "_source_updated_at": nano_timestamp(current.get("updated_at")),
            "_batch_id": sha256(delivery.encode()).hexdigest(),
            "_run_id": run_id,
        }

    def check_budget(self, target, key, payload):
        """Bound total fixed state bytes and per-table version markers."""
        row = self.connection.execute(
            "SELECT LENGTH(CAST(row_json AS BLOB)) FROM current_rows "
            "WHERE table_name=? AND row_key=?",
            (target, key),
        ).fetchone()
        total = self.connection.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(row_json AS BLOB))),0) "
            "FROM current_rows"
        ).fetchone()[0]
        size = len(payload.encode()) if payload is not None else 0
        prior = (row[0] or 0) if row else 0
        if total - prior + size > MAX_CURRENT_BYTES:
            raise ValueError("Fixed CDC current-state byte limit exceeded.")
        if row is None:
            count = self.connection.execute(
                "SELECT COUNT(*) FROM current_rows WHERE table_name=?",
                (target,),
            ).fetchone()[0]
            if count >= MAX_CURRENT_ROWS:
                raise ValueError("Fixed CDC row-version limit exceeded.")

    def counts(self, targets):
        """Count observed live rows without returning source payloads."""
        return {
            target: self.connection.execute(
                "SELECT COUNT(*) FROM current_rows WHERE table_name=? "
                "AND row_json IS NOT NULL",
                (target,),
            ).fetchone()[0]
            for target in targets
        }

    def completed_offsets(self):
        """Return internal Kafka checkpoints for bounded replay validation."""
        return {
            (topic, partition): offset
            for topic, partition, offset in self.connection.execute(
                "SELECT topic,partition_id,offset_id FROM offsets"
            )
        }

    def record_failure(self, record, code):
        """Save a fixed failure code without payload or driver text."""
        if not self.fixed_source:
            return
        if not re.fullmatch(r"[A-Z_]{1,64}", code):
            raise ValueError("Invalid CDC diagnostic code.")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO processing_errors VALUES (?,?,?,?,?)",
                (
                    record["topic"],
                    record["partition"],
                    record["offset"],
                    code,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def record_rebuild(self, request_id, status, snapshot_id=None, error=None):
        """Keep main-thread rebuild outcomes durably without private values."""
        if not self.fixed_source or not re.fullmatch(
            r"[0-9a-f]{32}", request_id
        ):
            raise ValueError("Invalid fixed CDC rebuild identity.")
        if status not in {"running", "succeeded", "failed"} or (
            error is not None and not re.fullmatch(r"[A-Z_]{1,64}", error)
        ):
            raise ValueError("Invalid CDC rebuild outcome.")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO rebuild_history VALUES (?,?,?,?,?)",
                (
                    request_id,
                    status,
                    snapshot_id,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def rows(self, target):
        """Return live business rows with the existing Bronze metadata
        fields.
        """
        result = []
        for payload, digest, timestamp in self.connection.execute(
            "SELECT row_json, source_hash, ingested_at FROM current_rows "
            "WHERE table_name = ? AND row_json IS NOT NULL ORDER BY row_key",
            (target,),
        ):
            result.append(
                {
                    **json.loads(payload),
                    "_source_file": "cdc/" + target,
                    "_source_hash": digest,
                    "_ingested_at": timestamp,
                }
            )
        return result

    def complete(self, records):
        """Advance local offsets only after every Iceberg operation
        succeeds.
        """
        with self.connection:
            for record in records:
                prior = self.offset(record["topic"], record["partition"])
                if record["offset"] > prior:
                    self.connection.execute(
                        "INSERT OR REPLACE INTO offsets VALUES (?, ?, ?)",
                        (
                            record["topic"],
                            record["partition"],
                            record["offset"],
                        ),
                    )
