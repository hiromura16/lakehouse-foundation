"""Materialize immutable, bounded fixed-SQL batches without DB credentials.

The raw JSON is content-addressed. An observation has a separate apply ID,
because a source can move from state A to B and back to A after a deletion.
The completed-observation ledger is written only after all tables and derived
SQL succeed. A partial failure leaves raw evidence and possibly mixed table
generations; a replay deterministically reconstructs the complete snapshot.

The worker's process lock serializes this module with existing ingestion.
This single-writer PoC has no distributed multi-table transaction.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 100000
MAX_HISTORY = 100000
MAX_CURRENT_BYTES = 64 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]
SQL_SOURCE_COLUMNS = (
    "_source_system",
    "_source_database",
    "_source_schema",
    "_source_table",
    "_source_pk",
    "_source_updated_at",
    "_batch_id",
    "_run_id",
)
LEDGER_COLUMNS = (
    "apply_id",
    "batch_id",
    "source_identity",
    "mode",
    "observed_at",
    "counts_json",
    "sql_statements",
)
PROGRESS_COLUMNS = (
    "apply_id",
    "batch_id",
    "source_identity",
    "mode",
    "observed_at",
    "status",
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
BUSINESS_KEY = re.compile(r"[!-~]{1,160}")
UTC_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z")


class SqlBatchError(ValueError):
    """Contain only fixed, public-safe diagnostic text."""


def timestamp(value):
    """Validate canonical datetime2(7) text without losing its last digit."""
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        raise SqlBatchError("Use a seven-digit UTC SQL observation.")
    try:
        datetime.fromisoformat(value[:19])
    except ValueError:
        raise SqlBatchError("Invalid SQL UTC observation.") from None
    return value


def apply_identifier(batch_id, observed_at):
    """Bind a stable source-content ID to one actual source observation."""
    if not isinstance(batch_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", batch_id
    ):
        raise SqlBatchError("Invalid SQL batch identifier.")
    return sha256(
        (batch_id + "\n" + timestamp(observed_at)).encode("ascii")
    ).hexdigest()


def unique_json(pairs):
    """Reject duplicate keys rather than accept an ambiguous batch contract."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise SqlBatchError("Duplicate SQL batch JSON key.")
        result[key] = value
    return result


def read_document(batch_id, landing):
    """Read only a known content-addressed file and its exact ready marker."""
    landing = Path(landing).absolute()
    directory = landing / "sql-batches"
    path = directory / (batch_id + ".json")
    marker = path.with_name(path.name + ".ready")
    if (
        landing.resolve().is_relative_to(ROOT)
        or any(part.is_symlink() for part in (directory, *directory.parents))
        or path.is_symlink()
        or marker.is_symlink()
        or not path.is_file()
        or not marker.is_file()
        or path.stat().st_size > MAX_BYTES
        or marker.stat().st_size > 65
    ):
        raise SqlBatchError("Use a completed, bounded SQL batch file.")
    with marker.open("rb") as handle:
        if handle.read(66) != (batch_id + "\n").encode("ascii"):
            raise SqlBatchError("The SQL completion marker does not match.")
    before = path.stat()
    with path.open("rb") as handle:
        payload = handle.read(MAX_BYTES + 1)
    after = path.stat()
    if (
        len(payload) > MAX_BYTES
        or sha256(payload).hexdigest() != batch_id
        or (before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise SqlBatchError(
            "SQL batch bytes changed or failed hash validation."
        )
    try:
        document = json.loads(payload, object_pairs_hook=unique_json)
    except (ValueError, UnicodeError):
        raise SqlBatchError("Invalid SQL batch JSON.") from None
    return payload, document


def validate_document(document, contracts):
    """Validate all six tables before any S3, catalog or Iceberg mutation."""
    expected = {
        "format_version",
        "source_system",
        "source_identity",
        "source_database",
        "mode",
        "tables",
    }
    if isinstance(document, dict) and document.get("mode") == "incremental":
        expected |= {
            "before_watermarks",
            "upper_bound",
            "overlap_seconds",
            "safety_lag_seconds",
        }
    if (
        not isinstance(document, dict)
        or set(document) != expected
        or type(document["format_version"]) is not int
        or document["format_version"] != 1
        or document["source_system"] != "sqlserver-fixed-demo"
        or document["mode"] not in {"snapshot", "incremental"}
        or not isinstance(document["source_identity"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", document["source_identity"])
        or not isinstance(document["source_database"], str)
        or not IDENTIFIER.fullmatch(document["source_database"])
        or document["source_database"].casefold()
        in {
            "master",
            "model",
            "msdb",
            "tempdb",
        }
        or not isinstance(document["tables"], dict)
        or set(document["tables"]) != {item["table"] for item in contracts}
    ):
        raise SqlBatchError("The SQL batch does not match the fixed source.")
    if document["mode"] == "incremental":
        validate_window(document, contracts)
    counts = {}
    for contract in contracts:
        name = contract["table"]
        table = document["tables"][name]
        schema = "lh_demo_erp" if name.startswith("erp_") else "lh_demo_mes"
        primary_key = contract["columns"][0]
        if (
            not isinstance(table, dict)
            or set(table) != {"schema", "table", "primary_key", "rows"}
            or table["schema"] != schema
            or table["table"] != name.split("_", 1)[1]
            or table["primary_key"] != primary_key
            or not isinstance(table["rows"], list)
            or len(table["rows"]) > MAX_ROWS
        ):
            raise SqlBatchError("A SQL batch table is outside the allowlist.")
        seen = set()
        for row in table["rows"]:
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "values",
                    "source_pk",
                    "source_updated_at",
                    "run_id",
                }
                or not isinstance(row["values"], dict)
                or set(row["values"]) != set(contract["columns"])
                or any(
                    value is not None
                    and (not isinstance(value, str) or len(value) > 16384)
                    for value in row["values"].values()
                )
                or not isinstance(row["source_pk"], str)
                or not BUSINESS_KEY.fullmatch(row["source_pk"])
                or row["values"][primary_key] != row["source_pk"]
                or row["source_pk"] in seen
                or not isinstance(row["run_id"], str)
                or not re.fullmatch(r"[0-9a-f]{12}", row["run_id"])
            ):
                raise SqlBatchError("Invalid or duplicate SQL business key.")
            timestamp(row["source_updated_at"])
            seen.add(row["source_pk"])
        counts[name] = len(seen)
    return counts


def subtract_seconds(value, seconds):
    """Move a UTC window boundary without rounding its seventh digit."""
    value = timestamp(value)
    try:
        shifted = datetime.fromisoformat(value[:19]) - timedelta(
            seconds=seconds
        )
    except (ValueError, OverflowError):
        raise SqlBatchError("Invalid SQL visibility window.") from None
    return shifted.strftime("%Y-%m-%dT%H:%M:%S") + value[19:]


def validate_window(document, contracts):
    """Require complete fixed-table baselines and a bounded overlap policy."""
    before = document["before_watermarks"]
    overlap, lag = document["overlap_seconds"], document["safety_lag_seconds"]
    if (
        not isinstance(before, dict)
        or set(before) != {source["table"] for source in contracts}
        or type(overlap) is not int
        or not 1 <= overlap <= 86400
        or type(lag) is not int
        or not 0 <= lag <= overlap
    ):
        raise SqlBatchError("Invalid SQL overlap policy or snapshot baseline.")
    timestamp(document["upper_bound"])
    generations = set()
    for checkpoint in before.values():
        if not isinstance(checkpoint, dict) or set(checkpoint) != {
            "timestamp",
            "primary_key",
            "snapshot_id",
        }:
            raise SqlBatchError("Invalid SQL checkpoint contract.")
        if checkpoint["timestamp"] is None:
            if checkpoint["primary_key"] is not None:
                raise SqlBatchError(
                    "An empty SQL watermark cannot have a key."
                )
        else:
            timestamp(checkpoint["timestamp"])
            if not isinstance(checkpoint["primary_key"], str) or not (
                BUSINESS_KEY.fullmatch(checkpoint["primary_key"])
            ):
                raise SqlBatchError("Invalid SQL checkpoint key.")
        batch_id = checkpoint["snapshot_id"]
        if not isinstance(batch_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", batch_id
        ):
            raise SqlBatchError("The SQL checkpoint has no completed batch.")
        generations.add(batch_id)
    if len(generations) != 1:
        raise SqlBatchError("SQL checkpoint rows have different generations.")


def table_rows(table, *, maximum=MAX_HISTORY):
    """Bound control/current table scans; reject silent PoC limit overflow."""
    rows = table.scan(limit=maximum + 1).to_arrow().to_pylist()
    if len(rows) > maximum:
        raise SqlBatchError("SQL batch state exceeds its PoC row limit.")
    return rows


def prior_state(catalog):
    """Read identity, completions and durable unfinished-apply state."""
    identities = []
    if catalog.table_exists("sql_control.identity"):
        identities = table_rows(
            catalog.load_table("sql_control.identity"), maximum=1
        )
    history = []
    if catalog.table_exists("sql_control.batches"):
        history = table_rows(catalog.load_table("sql_control.batches"))
    progress = []
    if catalog.table_exists("sql_control.progress"):
        progress = table_rows(
            catalog.load_table("sql_control.progress"), maximum=1
        )
    # Completion records cannot prove that current rows are clean if either
    # the durable source identity or dirty-state singleton was lost/restored
    # independently. Empty first-creation tables remain safe to initialize.
    if (history and (not identities or not progress)) or (
        progress and not identities
    ):
        raise SqlBatchError(
            "SQL control state is incomplete; restore it as one dataset."
        )
    if any(
        not isinstance(row.get("source_identity"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", row["source_identity"])
        for row in [*identities, *history, *progress]
    ):
        raise SqlBatchError("Invalid existing SQL source identity state.")
    for row in history:
        if row.get("mode") not in {
            "snapshot",
            "incremental",
        } or apply_identifier(
            row.get("batch_id"), row.get("observed_at")
        ) != row.get("apply_id"):
            raise SqlBatchError("Invalid SQL completion ledger.")
    for row in progress:
        if (
            row.get("status") not in {"applying", "idle"}
            or row.get("mode") not in {"snapshot", "incremental"}
            or apply_identifier(row.get("batch_id"), row.get("observed_at"))
            != row.get("apply_id")
            or (
                row["status"] == "idle"
                and not any(
                    item["apply_id"] == row["apply_id"] for item in history
                )
            )
        ):
            raise SqlBatchError("Invalid durable SQL apply state.")
    return identities, history, progress


def create_control(admin, catalog, identity):
    """Claim an empty SQL dataset identity before changing business tables."""
    if not catalog.namespace_exists("sql_control"):
        catalog.create_namespace("sql_control")
    for name, columns in (
        ("identity", ("source_identity",)),
        ("batches", LEDGER_COLUMNS),
        ("progress", PROGRESS_COLUMNS),
    ):
        identifier = "sql_control." + name
        if not catalog.table_exists(identifier):
            catalog.create_table(
                identifier,
                schema=admin.pa.schema(
                    [(field, admin.pa.string()) for field in columns]
                ),
                properties={"format-version": "2"},
            )
    table = catalog.load_table("sql_control.identity")
    existing = table_rows(table, maximum=1)
    if not existing:
        # Never adopt unmarked business rows, including a legacy/manual table.
        for contract in admin.CONTRACT:
            identifier = "sql_bronze." + contract["table"]
            if catalog.table_exists(identifier) and table_rows(
                catalog.load_table(identifier), maximum=MAX_ROWS
            ):
                raise SqlBatchError("Unclaimed SQL data must be reviewed.")
        table.append(
            admin.pa.Table.from_pylist(
                [{"source_identity": identity}],
                schema=table.schema().as_arrow(),
            )
        )


def set_progress(admin, catalog, observation, status):
    """Durably mark applying before writes; clear only after completion.

    This singleton is intentionally separate from the completed ledger. A
    failed overwrite/medallion can leave Bronze dirty while the last completed
    batch still has the same content ID as a new snapshot of the source.
    """
    table = catalog.load_table("sql_control.progress")
    row = {
        key: observation[key] for key in PROGRESS_COLUMNS if key != "status"
    }
    row["status"] = status
    table.overwrite(
        admin.pa.Table.from_pylist([row], schema=table.schema().as_arrow())
    )


def acknowledgement(row, *, skipped):
    """Return only bounded counts and public completion identifiers."""
    try:
        saved = json.loads(row["counts_json"])
        counts = (
            saved["selected"]
            if isinstance(saved, dict) and ("selected" in saved)
            else saved
        )
        statements = int(row["sql_statements"])
    except (ValueError, TypeError, KeyError):
        raise SqlBatchError("Invalid SQL completion counts.") from None
    if (
        not isinstance(counts, dict)
        or any(
            type(count) is not int or not 0 <= count <= MAX_ROWS
            for count in counts.values()
        )
        or not 0 <= statements <= 1000
    ):
        raise SqlBatchError("Invalid SQL completion counts.")
    result = {
        "batch_id": row["batch_id"],
        "apply_id": row["apply_id"],
        "observed_at": row["observed_at"],
        "completed": True,
        "mode": row["mode"],
        "skipped": skipped,
        "counts": counts,
        "sql_statements": statements,
    }
    if isinstance(saved, dict) and "selected" in saved:
        if set(saved) != {"selected", "changed", "current"}:
            raise SqlBatchError("Invalid SQL completion count groups.")
        for key, field in (
            ("changed", "changed_counts"),
            ("current", "current_counts"),
        ):
            values = saved[key]
            if (
                not isinstance(values, dict)
                or set(values) != set(counts)
                or any(
                    type(value) is not int or not 0 <= value <= MAX_ROWS
                    for value in values.values()
                )
            ):
                raise SqlBatchError("Invalid SQL completed row-change counts.")
            result[field] = dict(values)
            if key == "changed" and skipped:
                result[field] = {name: 0 for name in counts}
    return result


def bronze_rows(document, contract, batch_id, ingested_at):
    """Keep original scalar meaning and attach actual SQL source provenance."""
    name = contract["table"]
    source = document["tables"][name]
    return [
        {
            **row["values"],
            "_source_file": "sql-batches/" + batch_id + ".json",
            "_source_hash": batch_id,
            "_ingested_at": ingested_at,
            "_source_system": document["source_system"],
            "_source_database": document["source_database"],
            "_source_schema": source["schema"],
            "_source_table": source["table"],
            "_source_pk": row["source_pk"],
            "_source_updated_at": row["source_updated_at"],
            "_batch_id": batch_id,
            "_run_id": row["run_id"],
        }
        for row in source["rows"]
    ]


def materialization_plan(admin, catalog, document, batch_id, ingested_at):
    """Validate all current rows and calculate idempotent PK merges first."""
    planned, changes, totals = {}, {}, {}
    byte_budget = MAX_CURRENT_BYTES
    for contract in admin.CONTRACT:
        name = contract["table"]
        incoming = bronze_rows(document, contract, batch_id, ingested_at)
        if document["mode"] == "snapshot":
            rows = incoming
            changes[name] = len(incoming)
        else:
            identifier = "sql_bronze." + name
            if not catalog.table_exists(identifier):
                raise SqlBatchError(
                    "Restore the complete SQL snapshot baseline."
                )
            current = table_rows(
                catalog.load_table(identifier), maximum=MAX_ROWS
            )
            indexed = {}
            for row in current:
                key = row.get("_source_pk")
                if (
                    not isinstance(key, str)
                    or not BUSINESS_KEY.fullmatch(key)
                    or row.get(contract["columns"][0]) != key
                    or key in indexed
                    or row.get("_source_system") != document["source_system"]
                    or not isinstance(row.get("_source_database"), str)
                    or row["_source_database"].casefold()
                    != document["source_database"].casefold()
                    or row.get("_source_schema")
                    != document["tables"][name]["schema"]
                    or row.get("_source_table")
                    != document["tables"][name]["table"]
                ):
                    raise SqlBatchError(
                        "SQL current state does not match its source."
                    )
                timestamp(row.get("_source_updated_at"))
                indexed[key] = row
            changed = 0
            for row in incoming:
                key = row["_source_pk"]
                previous = indexed.get(key)
                if previous:
                    if (
                        row["_source_updated_at"]
                        < previous["_source_updated_at"]
                    ):
                        continue
                    unchanged = all(
                        previous.get(column) == row.get(column)
                        for column in (
                            *contract["columns"],
                            "_source_updated_at",
                            "_run_id",
                        )
                    )
                    if unchanged:
                        continue
                # Equal SQL clock values are ordered by the later observation.
                # This supports rapid same-key updates within the overlap.
                indexed[key] = row
                changed += 1
            rows = [indexed[key] for key in sorted(indexed)]
            changes[name] = changed
        if len(rows) > MAX_ROWS:
            raise SqlBatchError("SQL current state exceeds its PoC row limit.")
        for row in rows:
            byte_budget -= len(
                json.dumps(row, ensure_ascii=False).encode("utf-8")
            )
            if byte_budget < 0:
                raise SqlBatchError(
                    "SQL current state exceeds its memory bound."
                )
        planned[name] = rows
        totals[name] = len(rows)
    return planned, changes, totals


def load(admin, batch_id, observed_at, *, landing=Path("/landing")):
    """Materialize one snapshot and acknowledge its exact observed apply ID.

    The caller holds admin.LOCK. Raw archive, individual Iceberg tables,
    derived SQL and the completed ledger are independent commits. An error
    leaves the ledger unchanged, so the same observation can be replayed.
    """
    apply_id = apply_identifier(batch_id, observed_at)
    payload, document = read_document(batch_id, landing)
    counts = validate_document(document, admin.CONTRACT)
    if document["mode"] == "incremental":
        if document["upper_bound"] != subtract_seconds(
            observed_at, document["safety_lag_seconds"]
        ):
            raise SqlBatchError(
                "SQL visibility bound does not match observation."
            )
        for name, table in document["tables"].items():
            checkpoint = document["before_watermarks"][name]
            lower = (
                subtract_seconds(
                    checkpoint["timestamp"], document["overlap_seconds"]
                )
                if checkpoint["timestamp"]
                else "0001-01-01T00:00:00.0000000Z"
            )
            if any(
                not lower
                <= row["source_updated_at"]
                <= document["upper_bound"]
                for row in table["rows"]
            ):
                raise SqlBatchError(
                    "An incremental row is outside its visibility window."
                )
    if any(
        row["source_updated_at"] > observed_at
        for table in document["tables"].values()
        for row in table["rows"]
    ):
        raise SqlBatchError("SQL source revisions are newer than observation.")
    catalog = admin.catalog()
    identities, history, progress = prior_state(catalog)
    if not history and not progress:
        for contract in admin.CONTRACT:
            identifier = "sql_bronze." + contract["table"]
            if (
                catalog.table_exists(identifier)
                and catalog.load_table(identifier)
                .scan(limit=1)
                .to_arrow()
                .num_rows
            ):
                raise SqlBatchError(
                    "Unclaimed SQL data or missing apply state needs review."
                )
    for row in history:
        recorded = acknowledgement(row, skipped=True)
        if set(recorded["counts"]) != set(counts):
            raise SqlBatchError("SQL completion table counts are incomplete.")
    identity = document["source_identity"]
    if any(
        row["source_identity"] != identity
        for row in [*identities, *history, *progress]
    ):
        raise SqlBatchError("The SQL dataset belongs to a different source.")
    pending = next(
        (row for row in progress if row["status"] == "applying"), None
    )
    if document["mode"] == "incremental":
        baseline = next(iter(document["before_watermarks"].values()))[
            "snapshot_id"
        ]
        if not any(row["mode"] == "snapshot" for row in history) or not any(
            row["batch_id"] == baseline for row in history
        ):
            raise SqlBatchError(
                "A completed full SQL snapshot baseline is required."
            )
        if pending and pending["mode"] == "snapshot":
            raise SqlBatchError(
                "Reconcile the unfinished full snapshot before increments."
            )
    exact = [row for row in history if row["apply_id"] == apply_id]
    if exact:
        if len(exact) != 1:
            raise SqlBatchError(
                "Duplicate SQL completion records need review."
            )
        if pending:
            if pending["apply_id"] != apply_id:
                raise SqlBatchError(
                    "An unfinished SQL observation requires reconciliation."
                )
            # The ledger was committed but clearing the progress marker
            # failed. No other apply may be acknowledged until this clears.
            set_progress(admin, catalog, exact[0], "idle")
        return acknowledgement(exact[0], skipped=True)
    latest = max(history, key=lambda row: row["observed_at"], default=None)
    newest = max(
        [row for row in (latest, pending) if row is not None],
        key=lambda row: row["observed_at"],
        default=None,
    )
    if newest and (
        observed_at < newest["observed_at"]
        or (
            observed_at == newest["observed_at"]
            and batch_id != newest["batch_id"]
        )
    ):
        raise SqlBatchError(
            "A stale SQL observation cannot replace current data."
        )
    if len(history) >= MAX_HISTORY:
        raise SqlBatchError("SQL observation history reached its PoC limit.")
    planned, changed_counts, current_counts = materialization_plan(
        admin,
        catalog,
        document,
        batch_id,
        datetime.now(timezone.utc).isoformat(),
    )
    # All source structures and current identity were checked before mutation.
    admin.S3.put_object(
        Bucket=admin.os.environ["S3_BUCKET_RAW"],
        Key="sql-batches/" + batch_id + ".json",
        Body=payload,
        ContentType="application/json",
    )
    create_control(admin, catalog, identity)
    observation = {
        "apply_id": apply_id,
        "batch_id": batch_id,
        "source_identity": identity,
        "mode": document["mode"],
        "observed_at": observed_at,
    }
    # A pending apply means some business tables may already contain another
    # generation. Matching the last completed content is then insufficient.
    unchanged = bool(
        not pending
        and (
            (
                document["mode"] == "snapshot"
                and latest
                and latest["batch_id"] == batch_id
            )
            or (
                document["mode"] == "incremental"
                and not any(changed_counts.values())
            )
        )
    )
    if unchanged:
        changed_counts = {name: 0 for name in counts}
    set_progress(admin, catalog, observation, "applying")
    statements = 0
    if not unchanged:
        admin.ensure_tables("sql_")
        for contract in admin.CONTRACT:
            name = contract["table"]
            table = catalog.load_table("sql_bronze." + name)
            rows = planned[name]
            table.overwrite(
                admin.pa.Table.from_pylist(
                    rows, schema=table.schema().as_arrow()
                )
            )
        result = admin.medallion("sql_")
        statements = result["sql_statements"]
        # Check the committed Bronze counts and key uniqueness.
        for contract in admin.CONTRACT:
            name = contract["table"]
            rows = table_rows(
                catalog.load_table("sql_bronze." + name), maximum=MAX_ROWS
            )
            keys = [row[contract["columns"][0]] for row in rows]
            if len(rows) != current_counts[name] or len(set(keys)) != len(
                keys
            ):
                raise SqlBatchError("SQL source and Bronze counts differ.")
    completion = {
        "apply_id": apply_id,
        "batch_id": batch_id,
        "source_identity": identity,
        "mode": document["mode"],
        "observed_at": observed_at,
        "counts_json": json.dumps(
            {
                "selected": counts,
                "changed": changed_counts,
                "current": current_counts,
            },
            sort_keys=True,
        ),
        "sql_statements": str(statements),
    }
    # Complete the data ledger before clearing the durable dirty-state flag.
    # If either operation fails, no success acknowledgement leaves the worker.
    ledger = catalog.load_table("sql_control.batches")
    ledger.append(
        admin.pa.Table.from_pylist(
            [completion], schema=ledger.schema().as_arrow()
        )
    )
    set_progress(admin, catalog, completion, "idle")
    return acknowledgement(completion, skipped=unchanged)
