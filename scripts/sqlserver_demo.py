"""Own a fixed, synthetic SQL Server source without granting runtime DDL.

The six business tables mirror the Landing contracts. SQL ``updated_at`` is a
server UTC watermark; the original business revision stays in original_values.
Unrepresentable non-key scalars become typed NULL while the complete original
JSON remains available to Bronze/quarantine. A missing or oversized key is
rejected because a stable primary key cannot be invented safely.

Only the DBA SQL files create objects or grant permissions. Runtime writers
must pass configuration, ownership, effective-permission and explicit-consent
checks before any DML. Driver errors are replaced with fixed safe messages.
"""

import hmac
import json
import math
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
SAFETY_CHECKER = "lh_demo_ctl.check_write_safety_v1"
SAFETY_DEFINITION = (
    Path(__file__)
    .with_name("sqlserver_safety.sql")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
SCHEMAS = {
    "ERP": "lh_demo_erp",
    "MES": "lh_demo_mes",
    "CONTROL": "lh_demo_ctl",
}
CONTRACTS = json.loads(
    (ROOT / "medallion/sources.json").read_text(encoding="utf-8")
)["sources"]
TABLES = {
    source["table"]: {
        "schema": SCHEMAS[
            "ERP" if source["table"].startswith("erp_") else "MES"
        ],
        "table": source["table"].split("_", 1)[1],
        "columns": source["columns"],
        "pk": source["columns"][0],
    }
    for source in CONTRACTS
}
CAPTURE_INSTANCES = {
    name: item["schema"] + "_" + item["table"] + "_v1"
    for name, item in TABLES.items()
}
SYSTEM_DATABASES = {"master", "model", "msdb", "tempdb"}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
RUN_ID = re.compile(r"[0-9a-f]{12}")
TLS_LOCK = threading.RLock()
INTEGER_COLUMNS = {
    "planned_qty",
    "ordered_qty",
    "shipped_qty",
    "good_qty",
    "reject_qty",
}
DECIMAL_COLUMNS = {"standard_cycle_seconds": (18, 3), "unit_price": (18, 2)}
DATE_COLUMNS = {"planned_date", "order_date", "promised_date"}
TIME_COLUMNS = {"event_time", "shipped_at", "start_time", "end_time"}
TEXT_LENGTHS = {
    "product_name": 256,
    "product_family": 128,
    "unit": 32,
    "factory_id": 64,
    "reason_code": 64,
}


class SqlServerError(ValueError):
    """Carry only application-owned, private-value-free failure messages."""


def endpoint(values, prefix):
    """Return an internal comparison tuple; never return it through an API."""
    return (
        values.get(prefix + "_HOST", "").casefold(),
        values.get(prefix + "_PORT", "1433"),
        values.get(prefix + "_DATABASE", "").casefold(),
    )


def validate_settings(values, *, consent=False, role="demo"):
    """Validate fixed boundaries without connecting or interpreting env code.

    Consent is checked again by each write operation. Read-only diagnostics
    may inspect configuration before the user consents to changing SQL rows.
    The source reader and control writer share an endpoint, never credentials.
    """
    if role not in {"demo", "source", "control"} or type(consent) is not bool:
        raise SqlServerError("Invalid fixed SQL operation role or consent.")
    selected = dict(values)
    for key, schema in SCHEMAS.items():
        setting = "DEMO_SQLSERVER_" + key + "_SCHEMA"
        selected.setdefault(setting, schema)
        if selected[setting] != schema:
            raise SqlServerError(
                "Only the three fixed demo schemas are allowed."
            )
    for key, size in (("INSTALLATION_ID", 32), ("OWNERSHIP_TOKEN", 64)):
        value = selected.get("DEMO_SQLSERVER_" + key, "")
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9a-f]{" + str(size) + "}", value
        ):
            raise SqlServerError(
                "Configure the fixed demo installation identity."
            )
    shared = selected.get(
        "DEMO_SQLSERVER_ALLOW_SHARED_SOURCE_DATABASE", "false"
    )
    if shared not in {"true", "false"}:
        raise SqlServerError("Shared database flag must be true or false.")
    prefix = "DEMO_SQLSERVER" if role == "demo" else "SOURCE_SQLSERVER"
    for suffix in ("HOST", "DATABASE", "USER", "PASSWORD"):
        key = prefix + "_" + suffix
        if role == "control" and suffix in {"USER", "PASSWORD"}:
            key = "SOURCE_SQLSERVER_CONTROL_" + suffix
        value = selected.get(key, "")
        if (
            not isinstance(value, str)
            or not value
            or any(c in value for c in "\r\n\x00")
        ):
            raise SqlServerError(
                "Complete the selected fixed SQL credentials."
            )
    host = selected[prefix + "_HOST"]
    database = selected[prefix + "_DATABASE"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", host):
        raise SqlServerError("Invalid fixed SQL host setting.")
    if (
        not IDENTIFIER.fullmatch(database)
        or database.casefold() in SYSTEM_DATABASES
    ):
        raise SqlServerError("Select an existing non-system demo database.")
    port = selected.setdefault(prefix + "_PORT", "1433")
    if (
        not isinstance(port, str)
        or not port.isdigit()
        or not 1 <= int(port) <= 65535
    ):
        raise SqlServerError("Invalid fixed SQL port setting.")
    if (
        role == "demo"
        and database.casefold()
        == selected.get("SOURCE_SQLSERVER_DATABASE", "").casefold()
    ):
        if shared != "true" or endpoint(
            selected, "DEMO_SQLSERVER"
        ) != endpoint(selected, "SOURCE_SQLSERVER"):
            raise SqlServerError(
                "Shared source database is denied without an exact approved "
                "endpoint."
            )
    if selected.get(prefix + "_ENCRYPT", "true") != "true":
        raise SqlServerError("Fixed SQL connections require encryption.")
    trust = selected.get(prefix + "_TRUST_SERVER_CERTIFICATE", "false")
    if trust not in {"true", "false"}:
        raise SqlServerError(
            "SQL certificate trust flag must be true or false."
        )
    certificate = selected.get(prefix + "_CA_FILE", "") or "system"
    if certificate != "system" and (
        not Path(certificate).is_file()
        or any(c in certificate for c in "\r\n[]")
    ):
        raise SqlServerError(
            "SQL CA must be system trust or a regular PEM file."
        )
    selected[prefix + "_CA_FILE"] = certificate
    hostname = selected.get(prefix + "_HOSTNAME_IN_CERTIFICATE", "")
    if hostname and not re.fullmatch(r"[A-Za-z0-9_.-]{1,253}", hostname):
        raise SqlServerError("Invalid expected SQL certificate hostname.")
    return selected


@contextmanager
def connection(values, role="demo"):
    """Use encrypted process-local FreeTDS settings and role-specific auth.

    Certificate trust is verified by default. Explicit private PoC trust=true
    retains encryption but skips chain/name validation; it is never inferred
    from connection failures. The source role always requests read_only=True.
    All mutations remain uncommitted until the caller explicitly commits.
    """
    selected = validate_settings(values, role=role)
    prefix = "DEMO_SQLSERVER" if role == "demo" else "SOURCE_SQLSERVER"
    credentials = prefix if role != "control" else "SOURCE_SQLSERVER_CONTROL"
    with (
        TLS_LOCK,
        tempfile.TemporaryDirectory(prefix="fixed-sql-tls-") as name,
    ):
        path = Path(name) / "freetds.conf"
        trust = selected.get(prefix + "_TRUST_SERVER_CERTIFICATE", "false")
        lines = [
            "[global]",
            "    tds version = 7.4",
            "    encryption = require",
        ]
        if trust == "false":
            lines += [
                "    ca file = " + selected[prefix + "_CA_FILE"],
                "    check certificate hostname = yes",
            ]
            hostname = selected.get(prefix + "_HOSTNAME_IN_CERTIFICATE", "")
            if hostname:
                lines.append("    certificate hostname = " + hostname)
        lines += [
            "[lh_fixed_demo]",
            "    host = " + selected[prefix + "_HOST"],
            "    port = " + selected[prefix + "_PORT"],
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o600)
        previous = {
            key: os.environ.get(key) for key in ("FREETDS", "FREETDSCONF")
        }
        os.environ.update(FREETDS=str(path), FREETDSCONF=str(path))
        handle = None
        try:
            import pymssql

            handle = pymssql.connect(
                server="lh_fixed_demo",
                port=selected[prefix + "_PORT"],
                database=selected[prefix + "_DATABASE"],
                user=selected[credentials + "_USER"],
                password=selected[credentials + "_PASSWORD"],
                encryption="require",
                read_only=role == "source",
                autocommit=False,
                charset="UTF-8",
                login_timeout=15,
                timeout=60,
                appname="Lakehouse fixed synthetic source",
            )
            yield handle
        except SqlServerError:
            if handle is not None:
                try:
                    handle.rollback()
                except Exception:
                    pass
            raise
        except Exception:
            if handle is not None:
                try:
                    handle.rollback()
                except Exception:
                    pass
            raise SqlServerError(
                "Fixed SQL operation failed; private details withheld."
            ) from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    # A close error must not replace a safe failure with raw
                    # driver text or skip restoration of process TLS settings.
                    pass
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def table_name(contract):
    """Quote only a tracked table; no user schema/table/SQL is accepted."""
    if contract not in TABLES:
        raise SqlServerError("Only the six fixed source tables are allowed.")
    item = TABLES[contract]
    return "[" + item["schema"] + "].[" + item["table"] + "]"


def ownership(cursor, values, role="demo"):
    """Compare the connected database and immutable marker in constant time."""
    prefix = "DEMO_SQLSERVER" if role == "demo" else "SOURCE_SQLSERVER"
    cursor.execute("SELECT DB_NAME()")
    row = cursor.fetchone()
    if not row or row[0].casefold() != values[prefix + "_DATABASE"].casefold():
        raise SqlServerError(
            "The connected database did not match fixed settings."
        )
    cursor.execute("SELECT OBJECT_ID(N'lh_demo_ctl.installation', N'U')")
    if not cursor.fetchone()[0]:
        raise SqlServerError("The fixed demo installation marker is missing.")
    cursor.execute(
        "SELECT schema_version, installation_id, ownership_hash "
        "FROM [lh_demo_ctl].[installation] WHERE singleton_id = 1"
    )
    row = cursor.fetchone()
    expected_hash = sha256(
        values["DEMO_SQLSERVER_OWNERSHIP_TOKEN"].encode("ascii")
    ).hexdigest()
    if (
        not row
        or row[0] != SCHEMA_VERSION
        or not hmac.compare_digest(
            str(row[1]), values["DEMO_SQLSERVER_INSTALLATION_ID"]
        )
        or not hmac.compare_digest(str(row[2]), expected_hash)
    ):
        raise SqlServerError(
            "Fixed demo ownership or schema version did not match."
        )
    return SCHEMA_VERSION


def side_effects_safe(cursor, role="demo"):
    """Trust only the exact DBA-installed, read-only dbo metadata checker.

    Caller metadata can omit inbound legacy FKs. Never fall back to the
    caller's catalog views if this checker is missing, changed or fails.
    """
    if role == "source":
        return True
    cursor.execute(
        "SELECT m.definition, "
        "CASE WHEN m.execute_as_principal_id=DATABASE_PRINCIPAL_ID(N'dbo') "
        "AND COALESCE(o.principal_id,s.principal_id)="
        "DATABASE_PRINCIPAL_ID(N'dbo') AND m.uses_ansi_nulls=1 "
        "AND m.uses_quoted_identifier=1 THEN 1 ELSE 0 END "
        "FROM sys.sql_modules m JOIN sys.objects o "
        "ON m.object_id=o.object_id JOIN sys.schemas s "
        "ON o.schema_id=s.schema_id "
        "WHERE o.object_id=OBJECT_ID(%s,N'P') AND o.is_ms_shipped=0",
        (SAFETY_CHECKER,),
    )
    row = cursor.fetchone()
    if (
        not row
        or len(row) != 2
        or row[1] != 1
        or not isinstance(row[0], str)
        or row[0].replace("\r\n", "\n") != SAFETY_DEFINITION
    ):
        return False
    cursor.execute("EXEC [lh_demo_ctl].[check_write_safety_v1]", ())
    return cursor.fetchone() == (1,) and cursor.fetchone() is None


def permissions_safe(cursor, role="demo"):
    """Fail closed on broad roles, dangerous grants or unrelated object access.

    The writer's legacy grants are inspected, never revoked. A separate
    control principal is limited to watermark DML plus installation SELECT.
    The reader may have broad SELECT, but no runtime DML/DDL capability.
    Metadata visibility is evaluated as the actual connected principal.
    """
    roles = [
        "db_owner",
        "db_ddladmin",
        "db_datawriter",
        "db_securityadmin",
        "db_accessadmin",
    ]
    if role != "source":
        roles += ["db_datareader"]
    server_roles = [
        "sysadmin",
        "serveradmin",
        "securityadmin",
        "processadmin",
        "setupadmin",
        "bulkadmin",
        "diskadmin",
        "dbcreator",
    ]
    query = (
        "SELECT "
        + ", ".join("IS_MEMBER(N'" + name + "')" for name in roles)
        + ", "
        + ", ".join(
            "IS_SRVROLEMEMBER(N'" + name + "')" for name in server_roles
        )
    )
    cursor.execute(query)
    result = cursor.fetchone()
    if (
        not result
        or len(result) != len(roles) + len(server_roles)
        or any(value != 0 for value in result)
    ):
        return False
    for scope, dangerous in (
        (
            "DATABASE",
            (
                "CONTROL",
                "ALTER",
                "CREATE TABLE",
                "CREATE SCHEMA",
                "ALTER ANY USER",
                "ALTER ANY ROLE",
                "IMPERSONATE ANY USER",
                "TAKE OWNERSHIP",
            ),
        ),
        (
            "SERVER",
            (
                "CONTROL SERVER",
                "ALTER ANY LOGIN",
                "ALTER ANY SERVER ROLE",
                "IMPERSONATE ANY LOGIN",
            ),
        ),
    ):
        cursor.execute(
            "SELECT permission_name FROM fn_my_permissions(NULL, %s) "
            "WHERE permission_name IN ("
            + ",".join("%s" for _ in dangerous)
            + ")",
            (scope, *dangerous),
        )
        if cursor.fetchall():
            return False
    business = [
        item["schema"] + "." + item["table"] for item in TABLES.values()
    ]
    selectable = ["lh_demo_ctl.installation"]
    writable = []
    deletable = []
    if role == "demo":
        selectable += [*business, "lh_demo_ctl.demo_runs"]
        writable = [*business, "lh_demo_ctl.demo_runs"]
        deletable = business
    elif role == "control":
        selectable += ["lh_demo_ctl.batch_watermarks"]
        writable = ["lh_demo_ctl.batch_watermarks"]
    allowed_by_operation = {
        "SELECT": selectable,
        "INSERT": writable,
        "UPDATE": writable,
        "DELETE": deletable,
        "EXECUTE": [SAFETY_CHECKER] if role != "source" else [],
        "ALTER": [],
        "CONTROL": [],
        "TAKE OWNERSHIP": [],
    }
    if role == "source":
        allowed_by_operation.pop("SELECT")
    for operation, allowed in allowed_by_operation.items():
        # Use only object kinds to which the tested permission applies. For
        # example EXECUTE against a table can yield NULL rather than false.
        kinds = (
            "('P','FN','AF','FS')"
            if operation == "EXECUTE"
            else (
                "('U','V','IF','TF')"
                if operation == "SELECT"
                else (
                    "('U','V')"
                    if operation in {"INSERT", "UPDATE", "DELETE"}
                    else "('U','V','P','FN','IF','TF')"
                )
            )
        )
        check = (
            "HAS_PERMS_BY_NAME(QUOTENAME(s.name)+N'.'+QUOTENAME(o.name),"
            " N'OBJECT', %s)"
        )
        clause = check + " IS NULL OR (" + check + " = 1"
        arguments = [operation, operation]
        if allowed:
            clause += (
                " AND s.name+N'.'+o.name NOT IN ("
                + ",".join("%s" for _ in allowed)
                + ")"
            )
            arguments += allowed
        clause += ")"
        cursor.execute(
            "SELECT TOP (1) o.object_id FROM sys.objects o JOIN "
            "sys.schemas s ON o.schema_id=s.schema_id WHERE "
            "o.is_ms_shipped=0 AND o.type IN "
            + kinds
            + " AND ("
            + clause
            + ")",
            tuple(arguments),
        )
        if cursor.fetchone():
            return False
    # Object probes miss column-only grants, including those inherited
    # through roles and column GRANT overriding an object-level DENY.
    # Evaluate effective permissions, not only direct catalog grants.
    for operation in ("SELECT", "UPDATE"):
        if operation not in allowed_by_operation:
            continue
        allowed = allowed_by_operation[operation]
        check = (
            "HAS_PERMS_BY_NAME(QUOTENAME(s.name)+N'.'+QUOTENAME(o.name),"
            " N'OBJECT', %s, c.name, N'COLUMN')"
        )
        clause = check + " IS NULL OR (" + check + " = 1"
        arguments = [operation, operation]
        if allowed:
            clause += (
                " AND s.name+N'.'+o.name NOT IN ("
                + ",".join("%s" for _ in allowed)
                + ")"
            )
            arguments += allowed
        clause += ")"
        cursor.execute(
            "SELECT TOP (1) o.object_id, c.column_id FROM sys.columns c "
            "JOIN sys.objects o ON c.object_id=o.object_id "
            "JOIN sys.schemas s ON o.schema_id=s.schema_id WHERE "
            "o.is_ms_shipped=0 AND o.type IN "
            + ("('U','V','IF','TF')" if operation == "SELECT" else "('U','V')")
            + " AND ("
            + clause
            + ")",
            tuple(arguments),
        )
        if cursor.fetchone():
            return False
    # Effective schema-wide DDL remains unsafe even in an empty schema.
    cursor.execute(
        "SELECT TOP (1) s.schema_id FROM sys.schemas s WHERE s.name "
        "NOT IN (N'sys',N'INFORMATION_SCHEMA') AND "
        "(HAS_PERMS_BY_NAME(s.name,N'SCHEMA',N'ALTER')=1 OR "
        "HAS_PERMS_BY_NAME(s.name,N'SCHEMA',N'CONTROL')=1 OR "
        "HAS_PERMS_BY_NAME(s.name,N'SCHEMA',N'TAKE OWNERSHIP')=1)"
    )
    if cursor.fetchone():
        return False
    cursor.execute(
        "SELECT TOP (1) principal_id FROM sys.database_principals "
        "WHERE principal_id<>DATABASE_PRINCIPAL_ID() "
        "AND type IN ('S','U','G','E','X') AND "
        "HAS_PERMS_BY_NAME(QUOTENAME(name),N'USER',N'IMPERSONATE')=1"
    )
    if cursor.fetchone():
        return False
    required = [("lh_demo_ctl.installation", "SELECT")]
    if role in {"demo", "source"}:
        for item in TABLES.values():
            name = item["schema"] + "." + item["table"]
            required += [
                (name, operation)
                for operation in (
                    ("SELECT", "INSERT", "UPDATE", "DELETE")
                    if role == "demo"
                    else ("SELECT",)
                )
            ]
    if role == "demo":
        required += [
            ("lh_demo_ctl.demo_runs", operation)
            for operation in ("SELECT", "INSERT", "UPDATE")
        ]
    if role == "control":
        required += [
            ("lh_demo_ctl.batch_watermarks", operation)
            for operation in ("SELECT", "INSERT", "UPDATE")
        ]
    for name, operation in required:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(%s, N'OBJECT', %s)", (name, operation)
        )
        if cursor.fetchone() != (1,):
            return False
    return side_effects_safe(cursor, role)


def diagnose(values):
    """Return only allowlisted state, never connection names or driver text."""
    result = {
        "connected": False,
        "ownership": False,
        "schema_version": 0,
        "permissions_safe": False,
        "tables": {
            name: {
                "exists": False,
                "rows": None,
                "last_updated_at": None,
                "cdc_enabled": False,
                "capture_instance": None,
            }
            for name in TABLES
        },
    }
    try:
        selected = validate_settings(values)
        with connection(selected) as handle:
            result["connected"] = True
            cursor = handle.cursor()
            try:
                result["schema_version"] = ownership(cursor, selected)
                result["ownership"] = True
            except SqlServerError:
                return result
            result["permissions_safe"] = permissions_safe(cursor)
            for name in TABLES:
                target = table_name(name)
                cursor.execute("SELECT OBJECT_ID(%s, N'U')", (target,))
                exists = bool(cursor.fetchone()[0])
                result["tables"][name]["exists"] = exists
                if not exists:
                    continue
                cursor.execute(
                    "SELECT COUNT_BIG(*), MAX(CONVERT(varchar(33), updated_at,"
                    " 126)) FROM " + target
                )
                count, stamp = cursor.fetchone()
                result["tables"][name].update(
                    rows=int(count),
                    last_updated_at=(stamp + "Z") if stamp else None,
                )
                cursor.execute(
                    "SELECT is_tracked_by_cdc FROM sys.tables WHERE "
                    "object_id=OBJECT_ID(%s)",
                    (target,),
                )
                result["tables"][name]["cdc_enabled"] = bool(
                    cursor.fetchone()[0]
                )
                # Capture metadata may be unavailable to the narrow writer.
                try:
                    cursor.execute(
                        "SELECT capture_instance FROM cdc.change_tables WHERE "
                        "source_object_id=OBJECT_ID(%s)",
                        (target,),
                    )
                    captures = {row[0] for row in cursor.fetchall()}
                    if CAPTURE_INSTANCES[name] in captures:
                        result["tables"][name]["capture_instance"] = (
                            CAPTURE_INSTANCES[name]
                        )
                except Exception:
                    pass
    except Exception:
        # Unknown diagnostic fields stay false/None; never invent success.
        return result
    return result


def timestamp(value):
    """Preserve seven fractional digits while normalizing ISO text to UTC."""
    if not isinstance(value, str):
        raise ValueError("Timestamp must be an ISO string.")
    matched = re.fullmatch(
        "(\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d)(?:\\.(\\d{1,"
        "7}))?(Z|[+-]\\d\\d:\\d\\d)",
        value,
    )
    if not matched:
        raise ValueError("Timestamp needs an explicit UTC offset.")
    base, fraction, offset = matched.groups()
    parsed = datetime.fromisoformat(
        base + ("+00:00" if offset == "Z" else offset)
    )
    utc = parsed.astimezone(timezone.utc)
    return (
        utc.strftime("%Y-%m-%dT%H:%M:%S")
        + "."
        + (fraction or "").ljust(7, "0")
    )


def normalized_value(column, value):
    """Return one SQL-compatible scalar; keep the original separately."""
    if value is None:
        return None
    try:
        if column in INTEGER_COLUMNS:
            if isinstance(value, bool) or not re.fullmatch(
                r"[+-]?\d+", str(value)
            ):
                return None
            number = int(value)
            return number if -(2**63) <= number < 2**63 else None
        if column in DECIMAL_COLUMNS:
            if isinstance(value, bool):
                return None
            precision, scale = DECIMAL_COLUMNS[column]
            number = Decimal(str(value))
            if not number.is_finite() or abs(number) >= Decimal(10) ** (
                precision - scale
            ):
                return None
            quantum = Decimal(1).scaleb(-scale)
            quantized = number.quantize(quantum)
            return quantized if quantized == number else None
        if column in DATE_COLUMNS:
            return date.fromisoformat(value).isoformat()
        if column in TIME_COLUMNS:
            return timestamp(value)
        text = str(value)
        limit = TEXT_LENGTHS.get(column, 160)
        # SQL nvarchar lengths count UTF-16 code units, not Python codepoints.
        return text if len(text.encode("utf-16-le")) // 2 <= limit else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def original_json(row):
    """Serialize originals deterministically, preserving NULL and decimals."""

    def scalar(value):
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, float) and not math.isfinite(value):
            raise SqlServerError(
                "Non-finite fixed SQL source values are rejected."
            )
        if value is not None and not isinstance(
            value, (str, int, float, bool)
        ):
            raise SqlServerError("Unsupported fixed SQL source scalar.")
        return value

    return json.dumps(
        {key: scalar(value) for key, value in row.items()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def prepare_data(data):
    """Validate six contracts and collapse PK revisions deterministically."""
    if not isinstance(data, dict) or set(data) != set(TABLES):
        raise SqlServerError(
            "Supply all six fixed source contracts exactly once."
        )
    prepared = {}
    duplicates = normalized = 0
    for name, item in TABLES.items():
        rows = data[name]
        if not isinstance(rows, list) or len(rows) > 100000:
            raise SqlServerError("Fixed source rows must be a bounded list.")
        latest = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(item["columns"]):
                raise SqlServerError(
                    "Fixed source columns did not match their contract."
                )
            key = row[item["pk"]]
            if (
                not isinstance(key, str)
                or not key
                or normalized_value(item["pk"], key) is None
            ):
                raise SqlServerError(
                    "Fixed source needs a nonempty stable primary key."
                )
            payload = original_json(row)
            try:
                revision = timestamp(row["updated_at"])
            except (ValueError, TypeError):
                revision = ""
            candidate = (revision, payload, row)
            if key not in latest or candidate[:2] > latest[key][:2]:
                latest[key] = candidate
        duplicates += len(rows) - len(latest)
        output = []
        for key in sorted(latest):
            _, payload, original = latest[key]
            columns = [
                column for column in item["columns"] if column != "updated_at"
            ]
            typed = {
                column: normalized_value(column, original[column])
                for column in columns
            }
            normalized += sum(
                original[column] is not None and typed[column] is None
                for column in columns
            )
            output.append((key, typed, payload))
        prepared[name] = output
    return prepared, duplicates, normalized


def check_writer(cursor, values):
    """Check ownership and narrow effective permissions before any DML."""
    ownership(cursor, values)
    if not permissions_safe(cursor):
        raise SqlServerError(
            "Fixed demo writer permissions exceed the allowed boundary "
            "or are incomplete."
        )
    cursor.execute("SET XACT_ABORT ON")


def record_run(cursor, run_id, seed, scenario, days, count, target):
    """Record completion inside the same transaction as the source writes."""
    cursor.execute(
        "IF NOT EXISTS (SELECT 1 FROM [lh_demo_ctl].[demo_runs] "
        "WITH (UPDLOCK, HOLDLOCK) WHERE run_id=%s) INSERT INTO "
        "[lh_demo_ctl].[demo_runs] (run_id, seed, quality_mode, "
        "record_count, period_days, target_mode, status) VALUES (%s,"
        "%s,%s,%s,%s,%s,N'succeeded'); ELSE UPDATE "
        "[lh_demo_ctl].[demo_runs] SET record_count=%s, "
        "completed_at=SYSUTCDATETIME(), status=N'succeeded', "
        "target_mode=%s WHERE run_id=%s",
        (
            run_id,
            run_id,
            seed,
            scenario,
            count,
            days,
            target,
            count,
            target,
            run_id,
        ),
    )


def write_data(
    data, values, *, run_id, seed=42, scenario="clean", days=7, consent=False
):
    """Commit owned synthetic rows and skip identical replayed records."""
    if consent is not True:
        raise SqlServerError(
            "Explicit GUI/API consent is required for fixed SQL writes."
        )
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise SqlServerError(
            "Fixed demo run ID must contain 12 lowercase hex digits."
        )
    if (
        type(seed) is not int
        or not 0 <= seed <= 2147483647
        or scenario not in {"clean", "edge"}
        or type(days) is not int
        or not 1 <= days <= 366
    ):
        raise SqlServerError("Invalid fixed demo generation options.")
    selected = validate_settings(values, consent=consent)
    prepared, duplicates, normalized = prepare_data(data)
    result = {
        "run_id": run_id,
        "source_only": True,
        "counts": {name: len(rows) for name, rows in prepared.items()},
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "pk_deduplicated": duplicates,
        "normalized_values": normalized,
    }
    with connection(selected) as handle:
        cursor = handle.cursor()
        check_writer(cursor, selected)
        for name, rows in prepared.items():
            item = TABLES[name]
            target = table_name(name)
            primary = "[" + item["pk"] + "]"
            for key, typed, payload in rows:
                cursor.execute(
                    "SELECT run_id, original_values FROM "
                    + target
                    + " WITH (UPDLOCK,HOLDLOCK) WHERE "
                    + primary
                    + "=%s",
                    (key,),
                )
                prior = cursor.fetchone()
                if prior and prior[0] != run_id:
                    raise SqlServerError(
                        "A fixed source key belongs to another run; existing "
                        "data was preserved."
                    )
                if prior and prior[1] == payload:
                    result["skipped"] += 1
                    continue
                if prior:
                    changed = {
                        column: value
                        for column, value in typed.items()
                        if column != item["pk"]
                    }
                    cursor.execute(
                        "UPDATE "
                        + target
                        + " SET "
                        + ",".join("[" + column + "]=%s" for column in changed)
                        + ",original_values=%s,updated_at=SYSUTCDATETIME() "
                        "WHERE " + primary + "=%s AND run_id=%s",
                        (*changed.values(), payload, key, run_id),
                    )
                    result["updated"] += 1
                else:
                    columns = [*typed, "run_id", "original_values"]
                    cursor.execute(
                        "INSERT INTO "
                        + target
                        + " ("
                        + ",".join("[" + column + "]" for column in columns)
                        + ") VALUES ("
                        + ",".join("%s" for _ in columns)
                        + ")",
                        (*typed.values(), run_id, payload),
                    )
                    result["inserted"] += 1
        record_run(
            cursor,
            run_id,
            seed,
            scenario,
            days,
            sum(result["counts"].values()),
            "fixed-sqlserver",
        )
        handle.commit()
    return result


def mutate(values, action, *, run_id, consent=False):
    """Apply I/U/D only to the run's deterministic CDC test product."""
    if (
        consent is not True
        or not isinstance(action, str)
        or action not in {"insert", "update", "delete"}
    ):
        raise SqlServerError(
            "A fixed CDC action and explicit consent are required."
        )
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise SqlServerError(
            "Fixed CDC run ID must contain 12 lowercase hex digits."
        )
    selected = validate_settings(values, consent=consent)
    key = "CDC-" + run_id
    target = table_name("erp_products")
    with connection(selected) as handle:
        cursor = handle.cursor()
        check_writer(cursor, selected)
        cursor.execute(
            "SELECT run_id, original_values FROM "
            + target
            + " WITH (UPDLOCK,HOLDLOCK) WHERE product_id=%s",
            (key,),
        )
        previous = cursor.fetchone()
        if previous and previous[0] != run_id:
            raise SqlServerError(
                "The CDC test product is not owned by this run."
            )
        changed = 0
        if action == "delete":
            if previous:
                cursor.execute(
                    "DELETE FROM "
                    + target
                    + " WHERE product_id=%s AND run_id=%s",
                    (key, run_id),
                )
                changed = 1
        else:
            if action == "update" and not previous:
                raise SqlServerError(
                    "Insert the owned CDC test product before updating it."
                )
            if action == "insert" and previous:
                return {
                    "action": action,
                    "changed_rows": 0,
                    "run_id": run_id,
                    "source_only": True,
                }
            row = {
                "product_id": key,
                "product_name": "合成CDC"
                + ("更新" if action == "update" else "投入"),
                "product_family": "SYNTHETIC",
                "unit": "piece",
                "standard_cycle_seconds": "30.000",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            payload = original_json(row)
            if previous:
                cursor.execute(
                    "UPDATE "
                    + target
                    + " SET product_name=%s, original_values=%s, "
                    "updated_at=SYSUTCDATETIME() WHERE product_id=%s AND "
                    "run_id=%s",
                    (row["product_name"], payload, key, run_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO "
                    + target
                    + " (product_id,product_name,product_family,unit,"
                    "standard_cycle_seconds,run_id,original_values) VALUES "
                    "(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        key,
                        row["product_name"],
                        row["product_family"],
                        row["unit"],
                        Decimal("30.000"),
                        run_id,
                        payload,
                    ),
                )
            changed = 1
        record_run(
            cursor, run_id, 0, "clean", 1, changed, "fixed-cdc-" + action
        )
        handle.commit()
    return {
        "action": action,
        "changed_rows": changed,
        "run_id": run_id,
        "source_only": True,
    }
