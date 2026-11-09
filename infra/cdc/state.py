"""Keep durable CDC row versions and completed Kafka offsets in SQLite."""

import json
import math
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
        self.connection = sqlite3.connect(path)
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
        expected = sha256(
            json.dumps(identity, sort_keys=True).encode()
        ).hexdigest()
        prior = self.get("identity")
        if prior and prior != expected:
            raise ValueError("CDC state volume belongs to a different source.")
        self.set("identity", expected)

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
        if envelope.get("op") not in {"r", "c", "u", "d"}:
            raise ValueError("Unsupported CDC operation; raw event was kept.")
        source = envelope.get("source", {})
        if (
            source.get("schema") != mapping["source_schema"]
            or source.get("table") != mapping["source_table"]
        ):
            raise ValueError("CDC event origin does not match the mapping.")
        fields = mapping["columns"]
        primary_key = columns[0]
        key_column = next(key for key in fields if fields[key] == primary_key)
        key = json.loads(record["key"] or "{}")
        if set(key) != {key_column} or key[key_column] is None:
            raise ValueError("CDC requires a single declared business key.")
        key_value = str(key[key_column])
        current = (
            envelope["before"] if envelope["op"] == "d" else envelope["after"]
        )
        if not isinstance(current, dict) or any(
            key not in current for key in fields
        ):
            raise ValueError(
                "CDC source columns changed; review the contract."
            )
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
