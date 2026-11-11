"""Observe fixed SQL pipelines through bounded internal APIs and Gold reads.

No source SQL connection is made here. Counts and consumer caches describe
their own observation times; they do not constitute a global snapshot or an
end-to-end integration test. Missing observations remain unknown, never zero.
Raw CDC payloads, source names and transport exception text are not returned.
"""

import json
import math
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from poc_catalog import GOLD, SOURCE_DETAILS
from poc_quality import next_page
from pydantic import BaseModel, ConfigDict, Field, model_validator

IDS = tuple(SOURCE_DETAILS)
ORIGINS = {"batch": "sql", "cdc": "sqlcdc"}
TARGETS = {
    "consumer-status": ("GET", "http://cdc-consumer:8001/status"),
    "consumer-rebuild": ("POST", "http://cdc-consumer:8001/rebuild"),
    "connector-status": (
        "GET",
        "http://cdc-connect:8083/connectors/lakehouse-demo-sqlserver/status",
    ),
}
ERROR_CODES = {
    "CDC_STARTING",
    "CDC_REBUILD_REQUIRED",
    "CDC_QUERY_REVIEW_REQUIRED",
    "CDC_PROCESSING_FAILED",
    "CDC_KAFKA_ACK_FAILED",
    "CDC_REBUILD_FAILED",
}
LogicalTable = Literal[
    "erp_products",
    "erp_work_orders",
    "erp_sales_orders",
    "erp_shipments",
    "mes_events",
    "mes_downtime",
]
ReadState = Literal[
    "observed",
    "partial",
    "unavailable",
    "unobserved",
    "not_enabled",
    "not_configured",
    "stale",
]
Scalar = str | int | float | bool | None


class ObservationUnavailable(ValueError):
    """Expose only fixed diagnostics, never request URLs or response bodies."""


class RebuildBusy(ObservationUnavailable):
    """Represent the consumer's fixed conflict response."""


class RebuildUnknown(ObservationUnavailable):
    """The request may have queued despite an HTTP response timeout."""


class LayerCount(BaseModel):
    """Keep an unavailable layer count distinct from a measured empty table."""

    table: LogicalTable
    bronze_rows: int | None = Field(default=None, ge=0)
    silver_rows: int | None = Field(default=None, ge=0)
    quarantine_rows: int | None = Field(default=None, ge=0)


class BatchObservation(BaseModel):
    """Describe a recorded completion without its source identity or data."""

    batch_id: str
    apply_id: str
    mode: Literal["snapshot", "incremental"]
    observed_at: str
    counts: dict[LogicalTable, int]
    changed_counts: dict[LogicalTable, int] | None = None
    current_counts: dict[LogicalTable, int] | None = None
    sql_statements: int = Field(ge=0, le=1000)


class PipelineObservation(BaseModel):
    """Read counts, completion and dirty state as independent observations."""

    state: ReadState
    observed_at: str | None = None
    layers: list[LayerCount]
    history_observed: bool
    last_batch: BatchObservation | None = None
    progress_observed: bool
    pending: bool | None = None
    consistent_snapshot: Literal[False] = False


class PartitionObservation(BaseModel):
    """Return logical table IDs and offsets; raw topic names stay internal."""

    table: LogicalTable
    partition: int = Field(ge=0)
    completed_offset: int | None = None
    broker_low: int | None = None
    broker_high: int | None = None
    next_offset: int | None = None
    lag: int | None = None
    state: Literal["observed", "unavailable", "retention_gap", "broker_reset"]


class RebuildObservation(BaseModel):
    """Report queued work separately from an observed completed rebuild."""

    request_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    completed: bool
    error_code: str | None = None
    raw_snapshot_id: str | None = None


class ConsumerObservation(BaseModel):
    """Show cache age without inventing healthy offsets."""

    state: ReadState
    ready: bool | None = None
    blocked: bool | None = None
    busy: bool | None = None
    observation_time: str | None = None
    last_success: str | None = None
    error_code: str | None = None
    current_rows: dict[LogicalTable, int | None]
    partitions: list[PartitionObservation]
    raw_events_appended_since_start: int | None = None
    rebuild: RebuildObservation | None = None


class ConnectorObservation(BaseModel):
    """Drop Connect worker IDs and failure traces from the public status."""

    state: ReadState
    connector_state: str | None = None
    tasks: list[dict[str, str | int]]


class CdcObservation(BaseModel):
    """Keep service/cache observations separate from persisted table counts."""

    enabled: bool
    configured: bool
    consumer: ConsumerObservation
    connector: ConnectorObservation
    layers: list[LayerCount]
    layer_counts_observed: bool
    raw_rows: int | None = None
    raw_count_observed: bool
    consistent_snapshot: Literal[False] = False


class RebuildConsent(BaseModel):
    """Accept one strict confirmation, without paths, offsets or SQL."""

    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: bool

    @model_validator(mode="after")
    def confirmed(self):
        """Reject truthy strings/numbers and explicit false."""
        if self.confirm is not True:
            raise ValueError("Explicit rebuild confirmation is required.")
        return self


class RebuildQueued(BaseModel):
    """An accepted asynchronous request is not a completed reconstruction."""

    accepted: Literal[True]
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["queued"]
    completed: Literal[False]


class GoldPreview(BaseModel):
    """Return a fixed Gold snapshot excerpt, with exact decimal strings."""

    origin: Literal["batch", "cdc"]
    dataset: Literal["production", "sales"]
    table: str
    snapshot_id: str
    rows: list[dict[str, Scalar]] = Field(max_length=100)
    row_limit: int = Field(ge=1, le=100)
    complete_dataset: Literal[False] = False


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


def _bounded_json(response, *, deadline, maximum=65536):
    """Bound decoded bytes and check the deadline at every network chunk.

    Identity encoding prevents a compressed response from expanding before
    the byte check. Read timeouts are per read: the deadline can exceed its
    budget by at most one in-flight read, whose timeout is also bounded.
    """
    if response.headers.get("content-encoding", "identity") != "identity":
        raise ObservationUnavailable("Encoded observation is unsupported.")
    payload = bytearray()
    for chunk in response.iter_bytes():
        if time.monotonic() >= deadline:
            raise ObservationUnavailable("Observation deadline reached.")
        payload.extend(chunk)
        if len(payload) > maximum:
            raise ObservationUnavailable(
                "Observation response limit exceeded."
            )
    try:
        if time.monotonic() >= deadline:
            raise ObservationUnavailable("Observation deadline reached.")
        return json.loads(payload, parse_float=Decimal)
    except (ValueError, UnicodeError):
        raise ObservationUnavailable("Invalid observation JSON.") from None


def query_rows(statement, *, maximum=6):
    """Read fixed internal Trino pages with bounded polling and cancellation.

    Only application-owned query builders call this helper. No route accepts
    SQL, a catalog name or a result URL from its caller.
    """
    deadline = time.monotonic() + 10
    pending = None
    rows = []
    with httpx.Client(
        timeout=5,
        trust_env=False,
        follow_redirects=False,
        headers={
            "X-Trino-User": "lakehouse-sql-observer",
            "Accept-Encoding": "identity",
        },
    ) as client:
        try:
            method, url = "POST", "http://trino:8080/v1/statement"
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ObservationUnavailable(
                        "Observation deadline reached."
                    )
                with client.stream(
                    method,
                    url,
                    content=statement if method == "POST" else None,
                    timeout=min(5, remaining),
                ) as response:
                    if response.status_code != 200:
                        raise ObservationUnavailable(
                            "Trino observation unavailable."
                        )
                    payload = _bounded_json(response, deadline=deadline)
                if not isinstance(payload, dict):
                    raise ObservationUnavailable("Invalid observation page.")
                pending = (
                    next_page(payload["nextUri"])
                    if payload.get("nextUri")
                    else None
                )
                if payload.get("error") or not isinstance(
                    payload.get("data", []), list
                ):
                    raise ObservationUnavailable(
                        "Observation was not completed."
                    )
                rows.extend(payload.get("data", []))
                if len(rows) > maximum:
                    raise ObservationUnavailable(
                        "Observation row limit exceeded."
                    )
                if pending is None:
                    if time.monotonic() >= deadline:
                        raise ObservationUnavailable(
                            "Observation deadline reached."
                        )
                    return rows
                method, url = "GET", pending
        except Exception:
            if pending is not None:
                try:
                    client.delete(pending, timeout=2)
                except Exception:
                    pass
            raise ObservationUnavailable(
                "Internal observation unavailable."
            ) from None


def request_json(target, *, token="", payload=None):
    """Use only three literal internal endpoints and bounded JSON responses."""
    if target not in TARGETS:
        raise ObservationUnavailable("Unknown internal observation target.")
    method, url = TARGETS[target]
    headers = {"Accept-Encoding": "identity"}
    deadline = time.monotonic() + 5
    if target.startswith("consumer-"):
        if not token:
            raise ObservationUnavailable(
                "Consumer observation auth is unavailable."
            )
        headers["Authorization"] = "Bearer " + token
    try:
        with httpx.Client(
            timeout=5, trust_env=False, follow_redirects=False
        ) as client:
            with client.stream(
                method, url, headers=headers, json=payload
            ) as response:
                if (
                    target == "consumer-rebuild"
                    and response.status_code == 409
                ):
                    raise RebuildBusy(
                        "A consumer operation is already pending."
                    )
                expected = 202 if target == "consumer-rebuild" else 200
                if response.status_code != expected:
                    raise ObservationUnavailable(
                        "Internal service observation unavailable."
                    )
                try:
                    return _bounded_json(response, deadline=deadline)
                except ObservationUnavailable:
                    # A 202 may have queued the action even when its body is
                    # malformed, oversized or arrives after the read budget.
                    if target == "consumer-rebuild":
                        raise RebuildUnknown(
                            "The rebuild request outcome is unknown."
                        ) from None
                    raise
    except httpx.TransportError:
        if target == "consumer-rebuild":
            raise RebuildUnknown(
                "The rebuild request outcome is unknown."
            ) from None
        raise ObservationUnavailable(
            "Internal service observation unavailable."
        ) from None
    except (RebuildBusy, ObservationUnavailable):
        raise
    except Exception:
        raise ObservationUnavailable(
            "Internal service observation unavailable."
        ) from None


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


def pipeline_status(query=None):
    """Read batch counts, last completion and pending state independently."""
    query = query or query_rows
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


def rebuild_observation(value):
    """Validate request identity and fixed rebuild states."""
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("request_id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", value["request_id"])
        or value.get("status")
        not in {"queued", "running", "succeeded", "failed"}
        or type(value.get("completed")) is not bool
        or value["completed"] != (value["status"] == "succeeded")
        or value.get("error_code") not in ERROR_CODES | {None}
    ):
        raise ObservationUnavailable("Invalid consumer rebuild observation.")
    snapshot = value.get("raw_snapshot_id")
    if snapshot is not None:
        snapshot = str(safe_count(snapshot))
    return {
        "request_id": value["request_id"],
        "status": value["status"],
        "completed": value["completed"],
        "error_code": value.get("error_code"),
        "raw_snapshot_id": snapshot,
    }


def consumer_observation(value):
    """Filter the consumer cache; all unknown offsets remain null."""
    if not isinstance(value, dict) or value.get("mode") != "fixed-demo":
        raise ObservationUnavailable(
            "Fixed-source consumer state is unavailable."
        )
    if any(
        type(value.get(key)) is not bool
        for key in ("ready", "blocked", "busy")
    ):
        raise ObservationUnavailable("Invalid consumer lifecycle flags.")
    observed = safe_timestamp(value.get("observation_time"))
    last_success = safe_timestamp(value.get("last_success"))
    code = value.get("error_code")
    if code not in ERROR_CODES | {None}:
        raise ObservationUnavailable("Invalid consumer error observation.")
    current = safe_counts(value.get("current_rows"), nullable=True)
    partitions = value.get("partitions")
    if not isinstance(partitions, list) or len(partitions) > 64:
        raise ObservationUnavailable("Invalid consumer partition observation.")
    result = []
    seen = set()
    for row in partitions:
        if (
            not isinstance(row, dict)
            or row.get("table") not in IDS
            or row.get("state")
            not in {
                "observed",
                "unavailable",
                "retention_gap",
                "broker_reset",
            }
        ):
            raise ObservationUnavailable("Invalid logical consumer partition.")
        partition = safe_count(row.get("partition"))
        key = (row["table"], partition)
        if key in seen:
            raise ObservationUnavailable(
                "Duplicate consumer partition observations."
            )
        seen.add(key)
        item = {
            "table": row["table"],
            "partition": partition,
            "state": row["state"],
        }
        for field in (
            "completed_offset",
            "broker_low",
            "broker_high",
            "next_offset",
            "lag",
        ):
            item[field] = safe_count(
                row.get(field),
                nullable=True,
                minimum=-1 if field == "completed_offset" else 0,
            )
        bounds = (
            "completed_offset",
            "broker_low",
            "broker_high",
            "next_offset",
        )
        if any(item[name] is None for name in bounds):
            item["lag"] = None
            if item["state"] == "observed":
                item["state"] = "unavailable"
        if (
            item["state"] == "observed"
            and all(item[name] is not None for name in bounds)
            and (
                item["next_offset"] != item["completed_offset"] + 1
                or not item["broker_low"]
                <= item["next_offset"]
                <= item["broker_high"]
                or item["lag"]
                != max(0, item["broker_high"] - item["next_offset"])
            )
        ):
            raise ObservationUnavailable(
                "Inconsistent consumer lag observation."
            )
        result.append(item)
    state = "observed" if observed else "unobserved"
    if observed:
        observed_time = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - observed_time
        if age.total_seconds() < -5 or (
            last_success
            and datetime.fromisoformat(last_success.replace("Z", "+00:00"))
            > observed_time
        ):
            raise ObservationUnavailable("Invalid consumer cache time.")
        if age.total_seconds() > 60:
            state = "stale"
    return {
        "state": state,
        **{key: value[key] for key in ("ready", "blocked", "busy")},
        "observation_time": observed,
        "last_success": last_success,
        "error_code": code,
        "current_rows": current,
        "partitions": result,
        "raw_events_appended_since_start": safe_count(
            value.get("raw_events_appended_since_start")
        ),
        "rebuild": rebuild_observation(value.get("rebuild")),
    }


def connector_observation(value):
    """Retain Connect lifecycle states, excluding worker IDs and traces."""
    states = {
        "RUNNING",
        "PAUSED",
        "FAILED",
        "UNASSIGNED",
        "DESTROYED",
        "RESTARTING",
    }
    if (
        not isinstance(value, dict)
        or value.get("name") != "lakehouse-demo-sqlserver"
    ):
        raise ObservationUnavailable(
            "Fixed connector observation unavailable."
        )
    connector = value.get("connector", {})
    tasks = value.get("tasks")
    if (
        not isinstance(connector, dict)
        or connector.get("state") not in states
        or (not isinstance(tasks, list) or len(tasks) > 64)
    ):
        raise ObservationUnavailable(
            "Invalid connector lifecycle observation."
        )
    selected = []
    for row in tasks:
        if not isinstance(row, dict) or row.get("state") not in states:
            raise ObservationUnavailable("Invalid connector task observation.")
        selected.append(
            {"id": safe_count(row.get("id")), "state": row["state"]}
        )
    return {
        "state": "observed",
        "connector_state": connector["state"],
        "tasks": selected,
    }


def cdc_enabled(values):
    """Select only the explicitly enabled fixed-source CDC deployment."""
    return "cdc" in values.get("COMPOSE_PROFILES", "").split(",") and (
        values.get("CDC_SOURCE_MODE", "generic") == "fixed-demo"
    )


def cdc_status(values, query=None, requester=None):
    """Observe independent services and tables without claiming integration."""
    query, requester = query or query_rows, requester or request_json
    enabled = "cdc" in values.get("COMPOSE_PROFILES", "").split(",")
    configured = cdc_enabled(values) and bool(values.get("WORKER_TOKEN"))
    state = "not_enabled" if not enabled else "not_configured"
    consumer = {
        "state": state,
        "current_rows": {name: None for name in IDS},
        "partitions": [],
    }
    result = {
        "enabled": enabled,
        "configured": configured,
        "consumer": consumer,
        "connector": {"state": state, "tasks": []},
        "layers": empty_layers(),
        "layer_counts_observed": False,
        "raw_rows": None,
        "raw_count_observed": False,
        "consistent_snapshot": False,
    }
    if not configured:
        return result
    try:
        result["consumer"] = consumer_observation(
            requester("consumer-status", token=values["WORKER_TOKEN"])
        )
    except Exception:
        consumer["state"] = "unavailable"
    try:
        result["connector"] = connector_observation(
            requester("connector-status")
        )
    except Exception:
        result["connector"]["state"] = "unavailable"
    try:
        result["layers"] = read_layers("cdc", query)
        result["layer_counts_observed"] = True
    except Exception:
        pass
    try:
        rows = query(
            "SELECT count(*) FROM iceberg.sqlcdc_events.events", maximum=1
        )
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ObservationUnavailable(
                "Invalid raw event count observation."
            )
        result["raw_rows"] = safe_count(rows[0][0])
        result["raw_count_observed"] = True
    except Exception:
        pass
    return result


def context_exporter(values, datasets, limit, *, origin):
    """Read one fixed Gold projection via byte-bounded Trino HTTP pages.

    The main reference is observed first; the data query pins that exact
    snapshot. The two bounded queries are not a global SQL/CDC snapshot.
    Connection settings are deliberately ignored: the endpoint is literal.
    """
    del values
    if (
        origin not in ORIGINS
        or not isinstance(datasets, list)
        or len(datasets) != 1
        or datasets[0] not in GOLD
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ObservationUnavailable("Select one fixed Gold preview.")
    dataset = datasets[0]
    contract = GOLD[dataset]
    namespace = ORIGINS[origin] + "_gold"
    table = "iceberg." + namespace + "." + contract["table"]
    refs = query_rows(
        "SELECT snapshot_id FROM iceberg."
        + namespace
        + '."'
        + contract["table"]
        + "$refs\" WHERE name = 'main' LIMIT 1",
        maximum=1,
    )
    if len(refs) != 1 or not isinstance(refs[0], list) or len(refs[0]) != 1:
        raise ObservationUnavailable("Gold snapshot is unavailable.")
    snapshot = safe_count(refs[0][0])
    columns = list(contract["columns"])
    rows = query_rows(
        "SELECT "
        + ",".join(columns)
        + " FROM "
        + table
        + " FOR VERSION AS OF "
        + str(snapshot)
        + " ORDER BY "
        + ",".join(contract["primary_key"])
        + " LIMIT "
        + str(limit),
        maximum=limit,
    )
    if any(
        not isinstance(row, list) or len(row) != len(columns) for row in rows
    ):
        raise ObservationUnavailable("Invalid fixed Gold projection.")
    return {
        "datasets": [
            {
                "dataset": dataset,
                "table": table,
                "snapshot_id": str(snapshot),
                "rows": [dict(zip(columns, row)) for row in rows],
            }
        ]
    }


def json_scalar(value):
    """Preserve decimal precision and dates, rejecting unsupported objects."""
    if value is None or type(value) is bool:
        return value
    if isinstance(value, Decimal):
        if not value.is_finite() or abs(value.adjusted()) > 128:
            raise ObservationUnavailable("Unsupported Gold decimal value.")
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if type(value) is int and -(2**63) <= value <= 2**63 - 1:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= 1024:
        return value
    raise ObservationUnavailable("Unsupported bounded Gold scalar.")


def gold_preview(origin, dataset, limit=20, exporter=None):
    """Read one fixed Gold snapshot; unexpected/private columns are dropped."""
    if (
        origin not in ORIGINS
        or dataset not in GOLD
        or (type(limit) is not int or not 1 <= limit <= 100)
    ):
        raise ObservationUnavailable("Select a bounded fixed Gold preview.")
    result = (exporter or context_exporter)(
        {"BIND_IP": "trino", "TRINO_PORT": "8080"},
        [dataset],
        limit,
        origin=origin,
    )
    datasets = result.get("datasets") if isinstance(result, dict) else None
    if not isinstance(datasets, list) or len(datasets) != 1:
        raise ObservationUnavailable("The Gold snapshot is unavailable.")
    item = datasets[0]
    expected = "iceberg." + ORIGINS[origin] + "_gold." + GOLD[dataset]["table"]
    if (
        not isinstance(item, dict)
        or item.get("dataset") != dataset
        or item.get("table") != expected
        or not isinstance(item.get("snapshot_id"), str)
        or not re.fullmatch(r"[0-9]{1,19}", item["snapshot_id"])
        or int(item["snapshot_id"]) > 2**63 - 1
        or not isinstance(item.get("rows"), list)
        or len(item["rows"]) > limit
    ):
        raise ObservationUnavailable("Invalid fixed Gold snapshot result.")
    rows = []
    for row in item["rows"]:
        if not isinstance(row, dict) or not set(
            GOLD[dataset]["primary_key"]
        ) <= set(row):
            raise ObservationUnavailable("Gold row keys are unavailable.")
        rows.append(
            {
                key: json_scalar(row[key])
                for key in GOLD[dataset]["columns"]
                if key in row
            }
        )
    return {
        "origin": origin,
        "dataset": dataset,
        "table": expected,
        "snapshot_id": item["snapshot_id"],
        "rows": rows,
        "row_limit": limit,
        "complete_dataset": False,
    }


def observation_router(
    store,
    jobs,
    authorize,
    *,
    query=None,
    requester=None,
    preview_exporter=None,
):
    """Bind fixed observations to applied settings and API auth."""
    router = APIRouter(
        prefix="/sql-source",
        tags=["SQL pipelineの観測"],
        dependencies=[Depends(authorize)],
    )

    @router.get("/pipeline-status", response_model=PipelineObservation)
    def batch_status():
        """Observe layer counts, last completion and unfinished progress."""
        return pipeline_status(query=query)

    @router.get("/cdc/status", response_model=CdcObservation)
    def cdc_observations():
        """Read service status and independent persisted CDC counts."""
        return cdc_status(store.applied, query=query, requester=requester)

    @router.post("/cdc/rebuild", response_model=RebuildQueued, status_code=202)
    def queue_rebuild(body: RebuildConsent):
        """Queue a consumer-owned raw rebuild; 202 never means completed."""
        del body
        if not cdc_enabled(store.applied):
            raise HTTPException(
                409,
                detail={
                    "code": "FIXED_CDC_NOT_ENABLED",
                    "message": "固定CDCのprofileとmodeを確認します。",
                },
            )
        if not jobs.operation.acquire(blocking=False):
            raise HTTPException(
                409,
                detail={
                    "code": "OPERATION_RUNNING",
                    "message": "実行中の操作が終わるまで待ちます。",
                },
            )
        try:
            value = (requester or request_json)(
                "consumer-rebuild",
                token=store.applied.get("WORKER_TOKEN", ""),
                payload={"confirm": True},
            )
            if (
                not isinstance(value, dict)
                or value.get("accepted") is not True
                or value.get("status") != "queued"
                or value.get("completed") is not False
                or not isinstance(value.get("request_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["request_id"])
            ):
                raise RebuildUnknown("Invalid rebuild acceptance.")
            return {
                key: value[key]
                for key in ("accepted", "request_id", "status", "completed")
            }
        except RebuildBusy:
            raise HTTPException(
                409,
                detail={
                    "code": "CDC_REBUILD_BUSY",
                    "message": "consumerの処理と再構築状態を確認します。",
                },
            ) from None
        except RebuildUnknown:
            raise HTTPException(
                503,
                detail={
                    "code": "CDC_REBUILD_RESULT_UNKNOWN",
                    "message": "受付結果が不明です。状態を確認します。",
                },
            ) from None
        except Exception:
            raise HTTPException(
                503,
                detail={
                    "code": "CDC_REBUILD_UNAVAILABLE",
                    "message": "consumerの状態と接続を確認します。",
                },
            ) from None
        finally:
            jobs.operation.release()

    @router.get("/preview", response_model=GoldPreview)
    def preview(
        origin: Literal["batch", "cdc"],
        dataset: Literal["production", "sales"] = "production",
        limit: int = Query(default=20, ge=1, le=100),
    ):
        """Read a finite Gold excerpt pinned to its current main snapshot."""
        try:
            return gold_preview(
                origin, dataset, limit, exporter=preview_exporter
            )
        except Exception:
            raise HTTPException(
                503,
                detail={
                    "code": "SQL_GOLD_PREVIEW_UNAVAILABLE",
                    "message": "対象Goldの処理とsnapshotを確認します。",
                },
            ) from None

    return router
