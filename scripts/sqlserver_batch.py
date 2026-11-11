"""Read the fixed SQL source and hand an immutable batch to the worker.

This module runs in the control API, where pymssql and private SOURCE settings
are available. The worker receives only a content ID and a server-observation
timestamp. It never receives source credentials or connects to SQL Server.

Snapshots hold shared locks on the six owned PoC tables in one bounded
SERIALIZABLE transaction. This deliberately favors a coherent, small PoC
snapshot over concurrency; it can block writers and is not a large-source
extraction strategy. No database option or source object is changed.
"""

import importlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100000
MAX_ORIGINAL_BYTES = 128 * 1024
MAX_FIELD_CHARS = 16384
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
BUSINESS_KEY = re.compile(r"[!-~]{1,160}")
TIMESTAMP = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[T ](?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,7}))?(?:Z)?"
)


class BatchError(ValueError):
    """Carry fixed, non-private errors across the source/API boundary."""


@lru_cache(maxsize=1)
def source_adapter():
    """Reuse the fixed writer's process-local FreeTDS lock and module."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("sqlserver_demo")


def canonical_timestamp(value):
    """Preserve all datetime2(7) digits from SQL CONVERT text, in UTC.

    datetime is used only to validate the calendar and clock. Formatting never
    round-trips through Python's microsecond representation or a float.
    """
    if not isinstance(value, str):
        raise BatchError("SQL timestamps must be precision-preserving text.")
    match = TIMESTAMP.fullmatch(value)
    if not match:
        raise BatchError("Invalid SQL UTC timestamp.")
    prefix = match["date"] + "T" + match["clock"]
    try:
        datetime.fromisoformat(prefix)
    except ValueError:
        raise BatchError("Invalid SQL UTC timestamp.") from None
    fraction = (match["fraction"] or "").ljust(7, "0")
    return prefix + "." + fraction + "Z"


def apply_identifier(batch_id, observed_at):
    """Identify an observation separately from its stable source content."""
    if not isinstance(batch_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", batch_id
    ):
        raise BatchError("Invalid SQL batch identifier.")
    stamp = canonical_timestamp(observed_at)
    if stamp != observed_at:
        raise BatchError("Use a canonical seven-digit UTC observation.")
    return sha256((batch_id + "\n" + stamp).encode("ascii")).hexdigest()


def unique_json(pairs):
    """Reject ambiguous duplicate keys in original-value source JSON."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise BatchError("Duplicate original-value JSON key.")
        result[key] = value
    return result


def original_values(payload, columns, primary_key, actual_key):
    """Keep NULL, empty strings and invalid original scalars distinct."""
    if (
        not isinstance(payload, str)
        or len(payload.encode("utf-8")) > MAX_ORIGINAL_BYTES
    ):
        raise BatchError("Original source JSON exceeds its bounded contract.")
    try:
        row = json.loads(payload, object_pairs_hook=unique_json)
    except (ValueError, TypeError):
        raise BatchError("Invalid original source JSON.") from None
    if (
        not isinstance(row, dict)
        or set(row) != set(columns)
        or any(
            value is not None
            and (not isinstance(value, str) or len(value) > MAX_FIELD_CHARS)
            for value in row.values()
        )
        or not isinstance(actual_key, str)
        or not BUSINESS_KEY.fullmatch(actual_key)
        or row[primary_key] != actual_key
    ):
        raise BatchError("Fixed source row does not match its declared key.")
    return row


def source_identity(values):
    """Hash the approved endpoint and installation without exporting them."""
    fields = (
        values["SOURCE_SQLSERVER_HOST"].casefold(),
        str(int(values.get("SOURCE_SQLSERVER_PORT", "1433"))),
        values["SOURCE_SQLSERVER_DATABASE"].casefold(),
        values["DEMO_SQLSERVER_INSTALLATION_ID"],
    )
    return sha256(
        json.dumps(fields, separators=(",", ":")).encode()
    ).hexdigest()


def batch_policy(values):
    """Bound the explicitly documented overlap and visibility safety window."""
    result = []
    for key, default, minimum, maximum in (
        ("SOURCE_SQLSERVER_BATCH_OVERLAP_SECONDS", "300", 1, 86400),
        ("SOURCE_SQLSERVER_BATCH_SAFETY_LAG_SECONDS", "5", 0, 3600),
    ):
        value = values.get(key, default)
        if (
            not isinstance(value, str)
            or not value.isdigit()
            or not (minimum <= int(value) <= maximum)
        ):
            raise BatchError("Invalid bounded SQL batch timing policy.")
        result.append(int(value))
    if result[0] < result[1]:
        raise BatchError("SQL overlap must cover the configured safety lag.")
    return tuple(result)


def subtract_seconds(value, seconds):
    """Subtract whole seconds while preserving the seventh fractional digit."""
    value = canonical_timestamp(value)
    try:
        shifted = datetime.fromisoformat(value[:19]) - timedelta(
            seconds=seconds
        )
    except (ValueError, OverflowError):
        raise BatchError(
            "SQL batch timing window is outside datetime2."
        ) from None
    return shifted.strftime("%Y-%m-%dT%H:%M:%S") + value[19:]


def validate_checkpoints(checkpoints, tables, *, required=False):
    """Refuse partial, unknown or internally inconsistent control rows."""
    if (
        not isinstance(checkpoints, dict)
        or (checkpoints and set(checkpoints) != set(tables))
        or (required and set(checkpoints) != set(tables))
    ):
        raise BatchError(
            "A complete six-table SQL snapshot baseline is required."
        )
    snapshots = set()
    for row in checkpoints.values():
        if not isinstance(row, dict) or set(row) != {
            "timestamp",
            "primary_key",
            "snapshot_id",
        }:
            raise BatchError("Invalid SQL batch checkpoint shape.")
        stamp, key = row["timestamp"], row["primary_key"]
        if stamp is None:
            if key is not None:
                raise BatchError("An empty SQL watermark cannot have a key.")
        elif (
            canonical_timestamp(stamp) != stamp
            or not isinstance(key, str)
            or not BUSINESS_KEY.fullmatch(key)
        ):
            raise BatchError("Invalid composite SQL watermark.")
        if not isinstance(row["snapshot_id"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", row["snapshot_id"]
        ):
            raise BatchError("The SQL checkpoint has no completed batch ID.")
        snapshots.add(row["snapshot_id"])
    if len(snapshots) > 1:
        raise BatchError(
            "SQL checkpoint rows are from different batch generations."
        )
    return checkpoints


def safe_landing(path):
    """Require an external landing directory without symlink parents."""
    path = Path(path).expanduser().absolute()
    if (
        path.resolve().is_relative_to(ROOT)
        or any(part.is_symlink() for part in (path, *path.parents))
        or (path.exists() and not path.is_dir())
    ):
        raise BatchError("Use a real external SQL batch landing directory.")
    return path


def publish_exact(path, payload):
    """Publish once atomically; reuse an existing byte-identical file only."""
    if path.is_symlink():
        raise BatchError("SQL batch files must not be symlinks.")
    if path.exists():
        if not path.is_file() or path.stat().st_size != len(payload):
            raise BatchError("Existing SQL batch bytes differ; preserve them.")
        with path.open("rb") as handle:
            if handle.read(len(payload) + 1) != payload:
                raise BatchError(
                    "Existing SQL batch bytes differ; preserve them."
                )
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sql-batch-", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            # Another producer may have published the same immutable content.
            publish_exact(path, payload)
    finally:
        temporary.unlink(missing_ok=True)


def read_snapshot(values, adapter, max_rows, *, checkpoints=None):
    """Read six full tables or a bounded overlap window under shared locks."""
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise BatchError("SQL snapshot row limit must be 1..100000.")
    selected = adapter.validate_settings(values, role="source")
    if selected.get("SOURCE_SQLSERVER_TIMEZONE", "UTC") != "UTC":
        raise BatchError("The fixed SQL source stores UTC timestamps.")
    document = {
        "format_version": 1,
        "source_system": "sqlserver-fixed-demo",
        "source_identity": source_identity(selected),
        "source_database": selected["SOURCE_SQLSERVER_DATABASE"],
        "mode": "incremental" if checkpoints is not None else "snapshot",
        "tables": {},
    }
    # Reserve ample JSON framing overhead while checking rows as they arrive.
    # The final exact byte check remains mandatory before file publication.
    budget = MAX_BYTES - 32768
    with adapter.connection(selected, role="source") as connection:
        cursor = connection.cursor()
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        adapter.ownership(cursor, selected, role="source")
        if not adapter.permissions_safe(cursor, role="source"):
            raise BatchError("The source reader must remain read-only.")
        # Acquire all table S locks first. Once the final lock is acquired,
        # every table remains unchanged until the read transaction finishes.
        for name, table in adapter.TABLES.items():
            primary_key = table["pk"]
            cursor.execute(
                "SELECT TOP (1) ["
                + primary_key
                + "] FROM "
                + adapter.table_name(name)
                + " WITH (TABLOCK, HOLDLOCK)"
            )
            cursor.fetchone()
        cursor.execute("SELECT CONVERT(varchar(27), SYSUTCDATETIME(), 126)")
        observed_at = canonical_timestamp(cursor.fetchone()[0])
        if checkpoints is not None:
            validate_checkpoints(checkpoints, adapter.TABLES, required=True)
            overlap, lag = batch_policy(selected)
            upper_bound = subtract_seconds(observed_at, lag)
            document.update(
                before_watermarks=checkpoints,
                overlap_seconds=overlap,
                safety_lag_seconds=lag,
                upper_bound=upper_bound,
            )
        for name, table in adapter.TABLES.items():
            origin = adapter.table_name(name)
            predicate = ""
            parameters = ()
            if checkpoints is not None:
                checkpoint = checkpoints[name]
                lower = (
                    subtract_seconds(checkpoint["timestamp"], overlap)
                    if checkpoint["timestamp"]
                    else "0001-01-01T00:00:00.0000000Z"
                )
                # The overlap cursor starts before all valid ASCII keys at
                # its timestamp. The persisted high cursor still includes PK.
                predicate = (
                    " WHERE ([updated_at] > CONVERT(datetime2(7), %s, 126) "
                    "OR ([updated_at] = CONVERT(datetime2(7), %s, 126) AND ["
                    + table["pk"]
                    + "] COLLATE Latin1_General_100_BIN2 > %s)) "
                    "AND [updated_at] <= CONVERT(datetime2(7), %s, 126)"
                )
                parameters = (lower[:-1], lower[:-1], "", upper_bound[:-1])
            cursor.execute(
                "SELECT COUNT_BIG(*) FROM " + origin + predicate,
                parameters or None,
            )
            expected = cursor.fetchone()[0]
            if type(expected) is not int or not 0 <= expected <= max_rows:
                raise BatchError("SQL snapshot exceeds its table row limit.")
            # Check large nvarchar(max) values before fetching any into the
            # 512 MiB API container; fixed typed columns are bounded by DDL.
            cursor.execute(
                "SELECT TOP (1) 1 FROM "
                + origin
                + " WHERE DATALENGTH([original_values]) > %s",
                (MAX_ORIGINAL_BYTES,),
            )
            if cursor.fetchone() is not None:
                raise BatchError("A source original-value row is too large.")
            cursor.execute(
                "SELECT TOP ("
                + str(max_rows + 1)
                + ") ["
                + table["pk"]
                + "], [original_values], "
                "CONVERT(varchar(27), [updated_at], 126), [run_id] FROM "
                + origin
                + predicate
                + " ORDER BY "
                + ("[updated_at], " if checkpoints is not None else "")
                + "["
                + table["pk"]
                + "] COLLATE Latin1_General_100_BIN2",
                parameters or None,
            )
            rows = []
            seen = set()
            while True:
                fetched = cursor.fetchmany(200)
                if not fetched:
                    break
                for key, payload, updated_at, run_id in fetched:
                    if (
                        key in seen
                        or not isinstance(run_id, str)
                        or not re.fullmatch(r"[0-9a-f]{12}", run_id)
                    ):
                        raise BatchError("Invalid source key or run metadata.")
                    row = {
                        "values": original_values(
                            payload, table["columns"], table["pk"], key
                        ),
                        "source_pk": key,
                        "source_updated_at": canonical_timestamp(updated_at),
                        "run_id": run_id,
                    }
                    if checkpoints is not None and not (
                        lower <= row["source_updated_at"] <= upper_bound
                    ):
                        raise BatchError(
                            "The SQL reader returned a row outside its window."
                        )
                    budget -= (
                        len(
                            json.dumps(
                                row, ensure_ascii=False, separators=(",", ":")
                            ).encode("utf-8")
                        )
                        + 1
                    )
                    if budget < 0 or len(rows) >= max_rows:
                        raise BatchError("SQL batch exceeds its global limit.")
                    seen.add(key)
                    rows.append(row)
            if len(rows) != expected:
                raise BatchError("SQL snapshot row counts did not agree.")
            # SQL BIN2 ordering is explicit; sorting also makes fake-reader
            # fixtures and repeat exports independent of driver fetch chunks.
            rows.sort(key=lambda row: row["source_pk"])
            document["tables"][name] = {
                "schema": table["schema"],
                "table": table["table"],
                "primary_key": table["pk"],
                "rows": rows,
            }
        connection.commit()  # Release read locks; no source DML was issued.
    return document, observed_at


def prepare_snapshot(
    values, landing, *, adapter=None, max_rows=MAX_ROWS, checkpoints=None
):
    """Publish a complete snapshot or internal checkpointed window document."""
    landing = safe_landing(landing)
    adapter = adapter or source_adapter()
    try:
        document, observed_at = read_snapshot(
            values, adapter, max_rows, checkpoints=checkpoints
        )
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise BatchError("SQL batch exceeds its global byte limit.")
        batch_id = sha256(payload).hexdigest()
        directory = safe_landing(landing / "sql-batches")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = directory / (batch_id + ".json")
        publish_exact(target, payload)
        publish_exact(
            target.with_name(target.name + ".ready"),
            (batch_id + "\n").encode("ascii"),
        )
        watermarks = {}
        for name, table in document["tables"].items():
            positions = [
                (row["source_updated_at"], row["source_pk"])
                for row in table["rows"]
            ]
            if checkpoints is not None and checkpoints[name]["timestamp"]:
                positions.append(
                    (
                        checkpoints[name]["timestamp"],
                        checkpoints[name]["primary_key"],
                    )
                )
            stamp, key = max(positions, default=(None, None))
            watermarks[name] = {"timestamp": stamp, "primary_key": key}
        return {
            "batch_id": batch_id,
            "observed_at": observed_at,
            "mode": document["mode"],
            "_watermarks": watermarks,
            "counts": {
                name: len(table["rows"])
                for name, table in document["tables"].items()
            },
        }
    except BatchError:
        raise
    except Exception:
        raise BatchError(
            "SQL snapshot failed; private source details withheld."
        ) from None


def validate_acknowledgement(result, prepared):
    """Require the exact completed observation before reporting success."""
    if (
        not isinstance(result, dict)
        or result.get("completed") is not True
        or result.get("batch_id") != prepared["batch_id"]
        or result.get("observed_at") != prepared["observed_at"]
        or result.get("apply_id")
        != apply_identifier(prepared["batch_id"], prepared["observed_at"])
        or result.get("counts") != prepared["counts"]
        or not isinstance(result.get("counts"), dict)
        or any(type(count) is not int for count in result["counts"].values())
        or type(result.get("skipped")) is not bool
        or type(result.get("sql_statements")) is not int
        or not 0 <= result["sql_statements"] <= 1000
    ):
        raise BatchError("The worker did not confirm this complete SQL batch.")
    public = {
        "batch_id": prepared["batch_id"],
        "apply_id": result["apply_id"],
        "observed_at": prepared["observed_at"],
        "mode": prepared["mode"],
        "completed": True,
        "skipped": result["skipped"],
        "counts": dict(prepared["counts"]),
        "sql_statements": result["sql_statements"],
    }
    for key in ("changed_counts", "current_counts"):
        if key in result:
            counts = result[key]
            if (
                not isinstance(counts, dict)
                or set(counts) != set(prepared["counts"])
                or any(
                    type(count) is not int or not 0 <= count <= MAX_ROWS
                    for count in counts.values()
                )
            ):
                raise BatchError("Invalid worker SQL row-change counts.")
            public[key] = dict(counts)
    return public


def inspect_checkpoints(values, *, adapter=None):
    """Read actual control positions without updates or source escalation.

    This explicit diagnostic uses the narrow control principal, not the
    SOURCE reader. It neither acquires update locks nor initializes missing
    rows. Hidden, partial or mixed-generation metadata is a safe failure.
    """
    configured = [
        bool(values.get("SOURCE_SQLSERVER_CONTROL_" + suffix))
        for suffix in ("USER", "PASSWORD")
    ]
    if not any(configured):
        return {
            "configured": False,
            "baseline_complete": False,
            "watermarks": {},
        }
    if not all(configured):
        raise BatchError("Complete the separate SQL control credentials.")
    adapter = adapter or source_adapter()
    try:
        selected = adapter.validate_settings(values, role="control")
        with adapter.connection(selected, role="control") as handle:
            cursor = handle.cursor()
            adapter.ownership(cursor, selected, role="control")
            if not adapter.permissions_safe(cursor, role="control"):
                raise BatchError("Narrow checkpoint rights are required.")
            cursor.execute(
                "SELECT TOP (7) [source_table], "
                "CONVERT(varchar(27), [last_watermark], 126), "
                "[last_primary_key], [snapshot_id] "
                "FROM [lh_demo_ctl].[batch_watermarks] ORDER BY [source_table]"
            )
            positions = {}
            for name, stamp, key, snapshot_id in cursor.fetchmany(7):
                if name in positions or name not in adapter.TABLES:
                    raise BatchError("Invalid checkpoint identity.")
                positions[name] = {
                    "timestamp": canonical_timestamp(stamp) if stamp else None,
                    "primary_key": key,
                    "snapshot_id": snapshot_id,
                }
            validate_checkpoints(positions, adapter.TABLES)
        return {
            "configured": True,
            "baseline_complete": set(positions) == set(adapter.TABLES),
            "watermarks": positions,
        }
    except Exception:
        raise BatchError(
            "SQL checkpoint observation failed; private details withheld."
        ) from None


@contextmanager
def checkpoint_lease(values, adapter):
    """Hold a narrow control transaction until worker acknowledgement.

    UPDLOCK/HOLDLOCK includes the empty-key range, serializing first snapshot
    initialization as well as updates. The source reader uses another login
    and connection; it never executes the DML below.
    """
    selected = adapter.validate_settings(values, role="control")
    with adapter.connection(selected, role="control") as handle:
        cursor = handle.cursor()
        cursor.execute("SET LOCK_TIMEOUT 5000")
        cursor.execute("SET XACT_ABORT ON")
        cursor.execute("SET NOCOUNT OFF")
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        adapter.ownership(cursor, selected, role="control")
        if not adapter.permissions_safe(cursor, role="control"):
            raise BatchError(
                "The checkpoint writer requires narrow control rights."
            )
        cursor.execute(
            "SELECT TOP (7) [source_table], "
            "CONVERT(varchar(27), [last_watermark], 126), "
            "[last_primary_key], [snapshot_id] "
            "FROM [lh_demo_ctl].[batch_watermarks] WITH (UPDLOCK, HOLDLOCK) "
            "ORDER BY [source_table]"
        )
        before = {}
        for name, stamp, key, snapshot_id in cursor.fetchmany(7):
            if name in before or name not in adapter.TABLES:
                raise BatchError(
                    "Unknown or duplicate SQL control checkpoint."
                )
            before[name] = {
                "timestamp": canonical_timestamp(stamp) if stamp else None,
                "primary_key": key,
                "snapshot_id": snapshot_id,
            }
        validate_checkpoints(before, adapter.TABLES)
        yield handle, cursor, before


def advance_checkpoints(handle, cursor, before, prepared):
    """CAS all six positions in one transaction after exact worker success."""
    for name, position in prepared["_watermarks"].items():
        stamp = position["timestamp"]
        sql_stamp = stamp[:-1] if stamp else None
        row = before.get(name)
        if row is None:
            cursor.execute(
                "INSERT INTO [lh_demo_ctl].[batch_watermarks] "
                "([source_table], [last_watermark], [last_primary_key], "
                "[snapshot_id]) VALUES "
                "(%s, CONVERT(datetime2(7), %s, 126), %s, %s)",
                (
                    name,
                    sql_stamp,
                    position["primary_key"],
                    prepared["batch_id"],
                ),
            )
        else:
            previous = row["timestamp"][:-1] if row["timestamp"] else None
            cursor.execute(
                "UPDATE [lh_demo_ctl].[batch_watermarks] "
                "SET [last_watermark]=CONVERT(datetime2(7), %s, 126), "
                "[last_primary_key]=%s, [snapshot_id]=%s, "
                "[updated_at]=SYSUTCDATETIME() WHERE [source_table]=%s "
                "AND ([last_watermark]=CONVERT(datetime2(7), %s, 126) "
                "OR ([last_watermark] IS NULL AND %s IS NULL)) "
                "AND ([last_primary_key]=%s "
                "OR ([last_primary_key] IS NULL AND %s IS NULL)) "
                "AND [snapshot_id]=%s",
                (
                    sql_stamp,
                    position["primary_key"],
                    prepared["batch_id"],
                    name,
                    previous,
                    previous,
                    row["primary_key"],
                    row["primary_key"],
                    row["snapshot_id"],
                ),
            )
        if cursor.rowcount != 1:
            raise BatchError(
                "The SQL checkpoint changed; no positions were advanced."
            )
    handle.commit()


def run_checkpointed(
    values, landing, worker_callback, adapter, max_rows, mode
):
    """Keep callback failures outside driver exception redaction."""
    callback_error = None
    failure = None
    result = None
    try:
        with checkpoint_lease(values, adapter) as (handle, cursor, before):
            try:
                if mode == "incremental":
                    validate_checkpoints(before, adapter.TABLES, required=True)
                prepared = prepare_snapshot(
                    values,
                    landing,
                    adapter=adapter,
                    max_rows=max_rows,
                    checkpoints=before if mode == "incremental" else None,
                )
                try:
                    reply = worker_callback(
                        prepared["batch_id"], prepared["observed_at"]
                    )
                except Exception as error:
                    # Preserve a controlled unknown-worker-outcome exception.
                    # Keep unknown worker outcomes distinct from SQL failures.
                    callback_error = error
                    raise BatchError(
                        "The worker did not acknowledge the SQL batch."
                    )
                result = validate_acknowledgement(reply, prepared)
                advance_checkpoints(handle, cursor, before, prepared)
            except Exception as error:
                failure = error
                try:
                    handle.rollback()
                except Exception:
                    pass
    except Exception:
        raise BatchError(
            "SQL control transaction failed; private details withheld."
        ) from None
    if callback_error is not None:
        raise callback_error
    if failure is not None:
        if isinstance(failure, BatchError):
            raise failure
        raise BatchError(
            "SQL batch checkpoint failed; inspect private control state."
        ) from None
    return {**result, "checkpointed": True}


def run_snapshot(
    values, landing, worker_callback, *, adapter=None, max_rows=MAX_ROWS
):
    """Read, publish and explicitly request the separate downstream action."""
    if any(
        values.get("SOURCE_SQLSERVER_CONTROL_" + suffix)
        for suffix in ("USER", "PASSWORD")
    ):
        return run_checkpointed(
            values,
            landing,
            worker_callback,
            adapter or source_adapter(),
            max_rows,
            "snapshot",
        )
    prepared = prepare_snapshot(
        values, landing, adapter=adapter, max_rows=max_rows
    )
    result = worker_callback(prepared["batch_id"], prepared["observed_at"])
    return {
        **validate_acknowledgement(result, prepared),
        "checkpointed": False,
    }


def run_incremental(
    values, landing, worker_callback, *, adapter=None, max_rows=MAX_ROWS
):
    """Merge a bounded window; physical deletes require full reconciliation.

    Timestamp+PK is not a commit LSN. Updates that commit after the overlap
    window has passed can still be missed. Use full snapshots or CDC to close
    that gap; this function never advertises complete change capture.
    """
    batch_policy(values)
    return run_checkpointed(
        values,
        landing,
        worker_callback,
        adapter or source_adapter(),
        max_rows,
        "incremental",
    )
