"""固定 SQL 読取の行を、画面に依存しない観測結果へ変換する。

許可された論理表と層だけを問い合わせる。通信や認可は呼出側に
任せ、欠けた観測をゼロや一貫した snapshot として扱わない。
"""

import json
import re
from datetime import datetime, timezone
from hashlib import sha256

from poc_catalog import SOURCE_DETAILS

IDS = tuple(SOURCE_DETAILS)
ORIGINS = {"batch": "sql", "cdc": "sqlcdc"}


class ObservationUnavailable(ValueError):
    """Expose only fixed diagnostics, never request URLs or response bodies."""


def safe_timestamp(value, *, required=False):
    """Validate UTC metadata before any backend string reaches a response."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?(?:Z|\+00:00)",
        value,
    ):
        raise ObservationUnavailable("Invalid UTC observation metadata.")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ObservationUnavailable(
            "Invalid UTC observation metadata."
        ) from None
    return value


def safe_count(value, *, nullable=False, minimum=0):
    """Reject booleans, negative values and unbounded integer payloads."""
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise ObservationUnavailable("Invalid observed count or offset.")
    return value


def safe_counts(values, *, nullable=False):
    """Require the six known logical table identifiers exactly once."""
    if not isinstance(values, dict) or set(values) != set(IDS):
        raise ObservationUnavailable("Invalid fixed-table observation.")
    return {name: safe_count(values[name], nullable=nullable) for name in IDS}


def empty_layers():
    """Unknown counts are null; an empty table must be observed as zero."""
    return [
        {
            "table": name,
            "bronze_rows": None,
            "silver_rows": None,
            "quarantine_rows": None,
        }
        for name in IDS
    ]


def count_statement(origin):
    """Build a fixed six-row count query from application identifiers."""
    if origin not in ORIGINS:
        raise ObservationUnavailable("Select a fixed SQL pipeline origin.")
    prefix = ORIGINS[origin]
    return " UNION ALL ".join(
        "SELECT '"
        + name
        + "', "
        + ", ".join(
            "(SELECT count(*) FROM iceberg."
            + prefix
            + "_"
            + layer
            + "."
            + name
            + ")"
            for layer in ("bronze", "silver", "quarantine")
        )
        for name in IDS
    )


def read_layers(origin, query):
    """Validate counts without reflecting Trino column names or error text."""
    rows = query(count_statement(origin), maximum=6)
    result = {}
    for row in rows:
        if (
            not isinstance(row, (list, tuple))
            or len(row) != 4
            or row[0] not in IDS
        ):
            raise ObservationUnavailable("Invalid pipeline layer counts.")
        if row[0] in result:
            raise ObservationUnavailable("Duplicate pipeline count rows.")
        result[row[0]] = {
            "table": row[0],
            **dict(
                zip(
                    ("bronze_rows", "silver_rows", "quarantine_rows"),
                    [safe_count(value) for value in row[1:]],
                )
            ),
        }
    if set(result) != set(IDS):
        raise ObservationUnavailable("Incomplete pipeline layer counts.")
    return [result[name] for name in IDS]


def latest_batch(rows):
    """Interpret only the six selected completion columns."""
    if not rows:
        return None
    if (
        len(rows) != 1
        or not isinstance(rows[0], (list, tuple))
        or len(rows[0]) != 6
    ):
        raise ObservationUnavailable(
            "Invalid SQL batch completion observation."
        )
    apply_id, batch_id, mode, observed_at, encoded, statements = rows[0]
    safe_timestamp(observed_at, required=True)
    if (
        mode not in {"snapshot", "incremental"}
        or not isinstance(batch_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", batch_id)
        or not isinstance(apply_id, str)
        or sha256((batch_id + "\n" + observed_at).encode()).hexdigest()
        != apply_id
        or not isinstance(encoded, str)
        or len(encoded) > 16384
        or not isinstance(statements, str)
        or not statements.isdigit()
        or not 0 <= int(statements) <= 1000
    ):
        raise ObservationUnavailable("Invalid SQL completion metadata.")
    saved = json.loads(encoded)
    counts = (
        saved["selected"]
        if isinstance(saved, dict) and "selected" in saved
        else saved
    )
    result = {
        "batch_id": batch_id,
        "apply_id": apply_id,
        "mode": mode,
        "observed_at": observed_at,
        "counts": safe_counts(counts),
        "changed_counts": None,
        "current_counts": None,
        "sql_statements": int(statements),
    }
    if isinstance(saved, dict) and "selected" in saved:
        if set(saved) != {"selected", "changed", "current"}:
            raise ObservationUnavailable(
                "Invalid SQL count observation groups."
            )
        result["changed_counts"] = safe_counts(saved["changed"])
        result["current_counts"] = safe_counts(saved["current"])
    return result


def pipeline_status(query):
    """Read batch counts, last completion and pending state independently."""
    result = {
        "state": "unavailable",
        "observed_at": None,
        "layers": empty_layers(),
        "history_observed": False,
        "last_batch": None,
        "progress_observed": False,
        "pending": None,
        "consistent_snapshot": False,
    }
    observed = 0
    try:
        result["layers"] = read_layers("batch", query)
        observed += 1
    except Exception:
        pass
    try:
        result["last_batch"] = latest_batch(
            query(
                "SELECT apply_id,batch_id,mode,observed_at,counts_json,"
                "sql_statements FROM iceberg.sql_control.batches "
                "ORDER BY observed_at DESC LIMIT 1",
                maximum=1,
            )
        )
        result["history_observed"] = True
        observed += 1
    except Exception:
        pass
    try:
        rows = query(
            "SELECT status FROM iceberg.sql_control.progress LIMIT 2",
            maximum=2,
        )
        if len(rows) > 1 or (
            rows
            and (len(rows[0]) != 1 or rows[0][0] not in {"applying", "idle"})
        ):
            raise ObservationUnavailable(
                "Invalid SQL apply-state observation."
            )
        result["pending"] = rows[0][0] == "applying" if rows else None
        result["progress_observed"] = True
        observed += 1
    except Exception:
        pass
    result["state"] = (
        "observed"
        if observed == 3
        else "partial"
        if observed
        else "unavailable"
    )
    if observed:
        result["observed_at"] = datetime.now(timezone.utc).isoformat()
    return result
