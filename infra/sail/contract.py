"""Fixed, synthetic Spark/Sail workload and exact comparison contract.

No function in this module opens a socket, starts an engine, or writes a
table. The runner consumes this same immutable input and these SQL recipes
for both engines. Timestamp strings and integer 100 ns ticks deliberately
avoid claiming that native Spark timestamps preserve SQL datetime2(7).
"""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

OWNER = "lakehouse-foundation-synthetic-lab-v1"
ENGINE_NAMESPACES = {"spark": "spark_lab", "sail": "sail_lab"}
MAX_ROWS = 10000
MAX_FIXTURE_BYTES = 2 * 1024 * 1024
TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(\d{1,7}))?(Z|[+-]\d{2}:\d{2})$"
)
SOURCES = {
    "erp_products": (
        ("product_id", "string"),
        ("product_name", "string"),
        ("product_family", "string"),
        ("unit", "string"),
        ("standard_cycle_seconds", "decimal(18,3)"),
        ("updated_at", "utc7"),
    ),
    "erp_work_orders": (
        ("work_order_id", "string"),
        ("factory_id", "string"),
        ("product_id", "string"),
        ("planned_date", "date"),
        ("planned_qty", "bigint"),
        ("updated_at", "utc7"),
    ),
    "erp_sales_orders": (
        ("sales_order_id", "string"),
        ("factory_id", "string"),
        ("product_id", "string"),
        ("order_date", "date"),
        ("promised_date", "date"),
        ("ordered_qty", "bigint"),
        ("unit_price", "decimal(18,2)"),
        ("updated_at", "utc7"),
    ),
    "erp_shipments": (
        ("shipment_id", "string"),
        ("sales_order_id", "string"),
        ("shipped_at", "utc7"),
        ("shipped_qty", "bigint"),
        ("updated_at", "utc7"),
    ),
    "mes_events": (
        ("event_id", "string"),
        ("work_order_id", "string"),
        ("event_time", "utc7"),
        ("good_qty", "bigint"),
        ("reject_qty", "bigint"),
        ("updated_at", "utc7"),
    ),
    "mes_downtime": (
        ("downtime_id", "string"),
        ("work_order_id", "string"),
        ("start_time", "utc7"),
        ("end_time", "utc7"),
        ("reason_code", "string"),
        ("updated_at", "utc7"),
    ),
}
GOLD_SCHEMA = (
    ("factory_id", "string"),
    ("production_date", "date"),
    ("planned_qty", "bigint"),
    ("good_qty", "bigint"),
    ("reject_qty", "bigint"),
    ("downtime_ticks_100ns", "bigint"),
    ("downtime_minutes", "decimal(28,9)"),
    ("total_qty", "bigint"),
    ("yield_pct", "decimal(18,2)"),
    ("plan_adherence_pct", "decimal(18,2)"),
)
TABLES = tuple(
    [f"{tier}_{source}" for tier in ("bronze", "silver") for source in SOURCES]
    + ["factory_daily_production"]
)
RUN_STAGES = (
    "input_checksum",
    "client_session",
    "catalog_ownership",
    "bronze_write",
    "silver_transform",
    "gold_transform",
    "iceberg_metadata",
    "engine_equality",
    "trino_equality",
    "replay",
    "session_stop",
)


class LabFailure(ValueError):
    """Expose a fixed diagnostic code, never an engine response or URL."""

    def __init__(self, code):
        if code not in CODES:
            raise ValueError("Unrecognized lab diagnostic code.")
        self.code = code
        super().__init__(code)


CODES = frozenset(
    {
        "FIXTURE_INVALID",
        "TIMESTAMP_PRECISION_UNSUPPORTED",
        "DESTINATION_REJECTED",
        "VALUE_CONTRACT_INVALID",
        "INPUT_CHANGED",
        "WRITE_APPROVAL_REQUIRED",
        "DEPENDENCY_UNAVAILABLE",
        "ENGINE_CONNECT_FAILED",
        "ENGINE_QUERY_FAILED",
        "CATALOG_UNAVAILABLE",
        "CATALOG_RESPONSE_INVALID",
        "CATALOG_OWNERSHIP_MISMATCH",
        "TABLE_FORMAT_UNSUPPORTED",
        "SCHEMA_MISMATCH",
        "VALUE_MISMATCH",
        "REPLAY_MISMATCH",
        "TRINO_READ_FAILED",
        "RESOURCE_EVIDENCE_PENDING",
        "STATE_UNAVAILABLE",
        "PREVIOUS_RUN_UNCERTAIN",
        "RUN_ACTIVE",
        "SESSION_STOP_FAILED",
        "NATIVE_TIMESTAMP7_UNSUPPORTED",
    }
)


def utc7(value):
    """Return exact UTC text and ticks since epoch without float arithmetic.

    Python datetime only handles microseconds; parsing whole seconds and the
    fraction separately prevents silent loss of the seventh decimal digit.
    Offsets are required, leap-second/naive/overprecision inputs are rejected.
    """
    if not isinstance(value, str) or not (match := TIMESTAMP.fullmatch(value)):
        raise LabFailure("FIXTURE_INVALID")
    base, fraction, offset = match.groups()
    try:
        if offset != "Z":
            hours, minutes = int(offset[1:3]), int(offset[4:6])
            if hours > 23 or minutes > 59:
                raise ValueError
        seconds = datetime.fromisoformat(base + offset.replace("Z", "+00:00"))
        seconds = seconds.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise LabFailure("FIXTURE_INVALID") from None
    fraction = (fraction or "").ljust(7, "0")
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = seconds - epoch
    ticks = (delta.days * 86400 + delta.seconds) * 10000000 + int(fraction)
    # strftime's %Y is not padded on every libc for years below 1000.
    stamp = seconds.isoformat(timespec="seconds").removesuffix("+00:00")
    return stamp + "." + fraction + "Z", ticks


def native_timestamp7_probe(value="2026-01-05T00:00:00.1234567Z"):
    """Fail reproducibly instead of narrowing 100 ns values to microseconds.

    Sail 0.7.3 documents unavailable Spark Connect result-schema conversion
    for timestamp_ns. This is a contract-level negative capability probe,
    not a measured server failure. No network or data write is performed.
    """
    _, ticks = utc7(value)
    if ticks % 10:
        raise LabFailure("NATIVE_TIMESTAMP7_UNSUPPORTED")
    return {"status": "not_run", "reason": "runtime_schema_unverified"}


def target(engine, table, catalog="lakehouse"):
    """Allow only a known engine, lab namespace, catalog alias and table."""
    if engine not in ENGINE_NAMESPACES or table not in TABLES:
        raise LabFailure("DESTINATION_REJECTED")
    if catalog not in {"lakehouse", "iceberg"}:
        raise LabFailure("DESTINATION_REJECTED")
    return f"{catalog}.{ENGINE_NAMESPACES[engine]}.{table}"


def fixture_bytes(path=None):
    """Read the bundled synthetic fixture and verify its immutable checksum."""
    path = path or Path(__file__).with_name("fixture.json")
    expected = Path(__file__).with_name("fixture.sha256").read_text().strip()
    content = path.read_bytes()
    if len(content) > MAX_FIXTURE_BYTES:
        raise LabFailure("FIXTURE_INVALID")
    digest = hashlib.sha256(content).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest != expected:
        raise LabFailure("INPUT_CHANGED")
    try:
        data = json.loads(content)
        validate_fixture(data)
    except (ValueError, TypeError, KeyError):
        raise LabFailure("FIXTURE_INVALID") from None
    return data, digest


def validate_fixture(data):
    """Require exactly the six known source shapes with bounded raw values."""
    if set(data) != {"schema_version", "provenance", "sources"}:
        raise LabFailure("FIXTURE_INVALID")
    if data["schema_version"] != 1 or set(data["sources"]) != set(SOURCES):
        raise LabFailure("FIXTURE_INVALID")
    count = 0
    for source, schema in SOURCES.items():
        rows = data["sources"][source]
        if not isinstance(rows, list) or not rows:
            raise LabFailure("FIXTURE_INVALID")
        for row in rows:
            count += 1
            if set(row) != {name for name, _ in schema}:
                raise LabFailure("FIXTURE_INVALID")
            for value in row.values():
                if value is not None and (
                    not isinstance(value, str) or len(value) > 1024
                ):
                    raise LabFailure("FIXTURE_INVALID")
        if count > MAX_ROWS:
            raise LabFailure("FIXTURE_INVALID")
    # Exact typed conversion validates ranges, date/timestamp and decimal loss.
    reference(data)


def schema_for(table):
    """Return an ordered schema with explicit timestamp text and ticks."""
    if table == "factory_daily_production":
        return GOLD_SCHEMA
    if table not in TABLES:
        raise LabFailure("DESTINATION_REJECTED")
    tier, source = table.split("_", 1)
    columns = []
    for name, kind in SOURCES[source]:
        columns.append((name, "string" if kind == "utc7" else kind))
        if kind == "utc7":
            columns.append((name + "_ticks_100ns", "bigint"))
    if tier == "bronze":
        columns = [(name, "string") for name, _ in columns]
    return tuple(columns)


def typed(value, kind):
    """Validate values exactly; never coerce floats/bools or round decimals."""
    if value is None:
        return None
    try:
        if kind == "string":
            if not isinstance(value, str):
                raise ValueError
            return value
        if kind == "bigint":
            if type(value) is int:
                integer = value
            elif isinstance(value, str) and re.fullmatch(r"-?\d+", value):
                integer = int(value)
            else:
                raise ValueError
            if not -(2**63) <= integer < 2**63:
                raise ValueError
            return integer
        if kind == "date":
            if isinstance(value, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", value
            ):
                return date.fromisoformat(value)
            if type(value) is date:
                return value
            raise ValueError
        match = re.fullmatch(r"decimal\((\d+),(\d+)\)", kind)
        if match:
            if not isinstance(value, (str, Decimal)):
                raise ValueError
            precision, scale = map(int, match.groups())
            result = Decimal(value)
            quantum = Decimal(1).scaleb(-scale)
            quantized = result.quantize(quantum)
            if not result.is_finite() or quantized != result:
                raise ValueError
            if abs(quantized) >= Decimal(10) ** (precision - scale):
                raise ValueError
            return quantized
    except (InvalidOperation, ValueError, OverflowError):
        raise LabFailure("VALUE_CONTRACT_INVALID") from None
    raise LabFailure("VALUE_CONTRACT_INVALID")


def bronze_rows(data, source):
    """Keep NULL/empty/text distinct; add exact UTC and 100 ns fields."""
    output = []
    for raw in data["sources"][source]:
        row = {}
        for name, kind in SOURCES[source]:
            value = raw[name]
            if kind == "utc7":
                if value is None:
                    raise LabFailure("VALUE_CONTRACT_INVALID")
                text, ticks = utc7(value)
                row[name] = text
                row[name + "_ticks_100ns"] = str(ticks)
            else:
                row[name] = value
        output.append(row)
    return output


def reference(data):
    """Compute an independent Python oracle, including duplicate semantics.

    Bronze retains duplicate multiplicity. Silver chooses the latest update
    by business key. Tied different revisions are rejected, rather than
    relying on a nondeterministic SQL row_number tie. The Gold recipe follows
    the existing independent per-grain aggregates and UTC start-date policy.
    """
    tables = {}
    for source in SOURCES:
        rows = bronze_rows(data, source)
        tables["bronze_" + source] = rows
        schema = schema_for("silver_" + source)
        primary = SOURCES[source][0][0]
        winners, revisions = {}, {}
        for row in rows:
            converted = {name: typed(row[name], kind) for name, kind in schema}
            key = converted[primary]
            if not key:
                raise LabFailure("FIXTURE_INVALID")
            prior = winners.get(key)
            stamp = converted["updated_at_ticks_100ns"]
            revision = (key, stamp)
            if revision in revisions and revisions[revision] != converted:
                raise LabFailure("FIXTURE_INVALID")
            revisions[revision] = converted
            if prior is None or stamp > prior["updated_at_ticks_100ns"]:
                winners[key] = converted
        tables["silver_" + source] = list(winners.values())
    work = {
        row["work_order_id"]: row for row in tables["silver_erp_work_orders"]
    }
    totals = defaultdict(lambda: [0, 0, 0, 0])
    for row in work.values():
        totals[row["factory_id"], row["planned_date"]][0] += row["planned_qty"]
    for row in tables["silver_mes_events"]:
        parent = work[row["work_order_id"]]
        key = parent["factory_id"], date.fromisoformat(row["event_time"][:10])
        totals[key][1] += row["good_qty"]
        totals[key][2] += row["reject_qty"]
    for row in tables["silver_mes_downtime"]:
        parent = work[row["work_order_id"]]
        key = parent["factory_id"], date.fromisoformat(row["start_time"][:10])
        duration = row["end_time_ticks_100ns"] - row["start_time_ticks_100ns"]
        if duration < 0:
            raise LabFailure("FIXTURE_INVALID")
        totals[key][3] += duration
    gold = []
    for (factory, day), (planned, good, reject, duration) in sorted(
        totals.items()
    ):

        def ratio(denominator):
            if denominator == 0:
                return None
            return (Decimal(100) * good / denominator).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        gold.append(
            dict(
                factory_id=factory,
                production_date=day,
                planned_qty=planned,
                good_qty=good,
                reject_qty=reject,
                downtime_ticks_100ns=duration,
                downtime_minutes=(
                    Decimal(duration) / Decimal(600000000)
                ).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP),
                total_qty=good + reject,
                yield_pct=ratio(good + reject),
                plan_adherence_pct=ratio(planned),
            )
        )
    tables["factory_daily_production"] = gold
    return tables


def canonical_row(table, row):
    """Encode all ordered typed business columns; NULL is never empty text."""
    schema = schema_for(table)
    if set(row) != {name for name, _ in schema}:
        raise LabFailure("SCHEMA_MISMATCH")
    fields = []
    for name, kind in schema:
        value = typed(row[name], kind)
        if isinstance(value, (date, Decimal)):
            value = str(value)
        fields.append([name, kind, value])
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def compare(table, expected, observed, difference_limit=5):
    """Compare full sorted multisets, not hashes alone or unordered sets.

    The bounded differences are safe only because both sides MUST be this
    synthetic contract. The public CLI intentionally emits hashes/counts,
    not the row differences; operators may inspect the local return object.
    """
    if not 0 <= difference_limit <= 5:
        raise LabFailure("VALUE_CONTRACT_INVALID")
    if len(expected) > MAX_ROWS or len(observed) > MAX_ROWS:
        raise LabFailure("VALUE_CONTRACT_INVALID")
    left = Counter(canonical_row(table, row) for row in expected)
    right = Counter(canonical_row(table, row) for row in observed)
    missing, extra = left - right, right - left

    def digest(rows):
        content = "\n".join(sorted(rows.elements())).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    return {
        "equal": left == right,
        "expected_count": len(expected),
        "observed_count": len(observed),
        "expected_sha256": digest(left),
        "observed_sha256": digest(right),
        "missing_count": sum(missing.values()),
        "extra_count": sum(extra.values()),
        "synthetic_diffs": {
            "missing": list(sorted(missing.elements()))[:difference_limit],
            "extra": list(sorted(extra.elements()))[:difference_limit],
        },
    }


def silver_select(source):
    """Use the same SQL casts/latest-revision rule in Spark and Sail."""
    if source not in SOURCES:
        raise LabFailure("DESTINATION_REJECTED")
    columns = ", ".join(
        f"CAST({name} AS {kind}) AS {name}"
        for name, kind in schema_for("silver_" + source)
    )
    primary = SOURCES[source][0][0]
    return (
        f"SELECT {columns} FROM (SELECT *, row_number() OVER "
        f"(PARTITION BY {primary} ORDER BY "
        "CAST(updated_at_ticks_100ns AS BIGINT) "
        f"DESC) AS revision FROM raw_{source}) AS ranked WHERE revision = 1"
    )


def gold_select():
    """Keep plan/events/downtime at one factory/day grain before joining.

    Unlike production's DOUBLE display values, the lab makes rate/minute
    precision explicit and also retains the exact integer duration ticks.
    This deliberate schema difference is part of the benchmark report.
    """
    return """
WITH plans AS (
  SELECT factory_id, planned_date AS production_date,
    sum(planned_qty) AS planned_qty
  FROM lab_work GROUP BY factory_id, planned_date
), production AS (
  SELECT w.factory_id,
    CAST(substr(e.event_time,1,10) AS DATE) AS production_date,
    sum(e.good_qty) AS good_qty, sum(e.reject_qty) AS reject_qty
  FROM lab_events e JOIN lab_work w ON e.work_order_id=w.work_order_id
  GROUP BY w.factory_id, CAST(substr(e.event_time,1,10) AS DATE)
), downtime AS (
  SELECT w.factory_id,
    CAST(substr(d.start_time,1,10) AS DATE) AS production_date,
    sum(d.end_time_ticks_100ns-d.start_time_ticks_100ns)
      AS downtime_ticks_100ns
  FROM lab_downtime d JOIN lab_work w ON d.work_order_id=w.work_order_id
  GROUP BY w.factory_id, CAST(substr(d.start_time,1,10) AS DATE)
), keys AS (
  SELECT factory_id,production_date FROM plans UNION
  SELECT factory_id,production_date FROM production UNION
  SELECT factory_id,production_date FROM downtime
), combined AS (
  SELECT k.factory_id,k.production_date,
    coalesce(p.planned_qty,0) AS planned_qty,
    coalesce(e.good_qty,0) AS good_qty,coalesce(e.reject_qty,0) AS reject_qty,
    coalesce(d.downtime_ticks_100ns,0) AS downtime_ticks_100ns
  FROM keys k LEFT JOIN plans p ON k.factory_id=p.factory_id
    AND k.production_date=p.production_date
  LEFT JOIN production e ON k.factory_id=e.factory_id
    AND k.production_date=e.production_date
  LEFT JOIN downtime d ON k.factory_id=d.factory_id
    AND k.production_date=d.production_date
)
SELECT factory_id,production_date,planned_qty,good_qty,reject_qty,
  downtime_ticks_100ns,
  CAST(round(CAST(downtime_ticks_100ns AS DECIMAL(28,0))/600000000,9)
    AS DECIMAL(28,9)) AS downtime_minutes,
  good_qty+reject_qty AS total_qty,
  CAST(round(CAST(good_qty AS DECIMAL(28,4))*100/
    nullif(good_qty+reject_qty,0),2)
    AS DECIMAL(18,2)) AS yield_pct,
  CAST(round(CAST(good_qty AS DECIMAL(28,4))*100/nullif(planned_qty,0),2)
    AS DECIMAL(18,2)) AS plan_adherence_pct
FROM combined
""".strip()
