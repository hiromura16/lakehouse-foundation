"""接続先を固定したSQL Serverの読取・watermark制御アダプター。

公開の6表・CDC契約および既存の所有権/TLS検証を維持する。
業務表の書込みは行わず、制御資格情報は別のcheckpoint表だけに使う。
"""

import hmac
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
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
# 新規制御用の厳密な定義。旧DBの同名定義は変更せず認識する。
CONTROL_SAFETY_DEFINITION = SAFETY_DEFINITION.replace(
    "N'lh_demo_ctl.installation',N'lh_demo_ctl.demo_runs',",
    "N'lh_demo_ctl.installation',",
).replace("))=9", "))=8")
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
# These are schema-contained OBJECT securables, not an applicability matrix
# for individual permission names. fn_my_permissions supplies the permissions
# that actually apply to each kind, including CLR routines and synonyms.
PERMISSION_OBJECT_TYPES = (
    "U",
    "V",
    "P",
    "PC",
    "X",
    "FN",
    "FS",
    "AF",
    "IF",
    "TF",
    "FT",
    "SN",
    "ET",
    "SO",
    "SQ",
    "R",
    "D",
)
# Child constraints, DML triggers and internal metadata cannot receive
# independent OBJECT grants. Standalone legacy defaults (D, parent=0) remain
# audited. Unknown visible user-object kinds are refused for manual review.
NON_GRANTABLE_CHILD_TYPES = (
    "C",
    "D",
    "F",
    "PK",
    "UQ",
    "EC",
    "TR",
    "TA",
    "IT",
    "ST",
)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
TLS_LOCK = threading.RLock()
TIME_COLUMNS = {"event_time", "shipped_at", "start_time", "end_time"}


class SqlServerError(ValueError):
    """Carry only application-owned, private-value-free failure messages."""


def validate_settings(values, *, role="source"):
    """固定読取と制御の設定だけを検証し、SQLには接続しない。"""
    if role not in {"source", "control"}:
        raise SqlServerError("Invalid fixed SQL operation role.")
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
    prefix = "SOURCE_SQLSERVER"
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
    if role == "control":
        # The source reader and the checkpoint writer cannot share auth.
        reader = selected.get("SOURCE_SQLSERVER_USER")
        control = selected["SOURCE_SQLSERVER_CONTROL_USER"]
        if (
            not isinstance(reader, str)
            or not reader
            or any(c in reader for c in "\r\n\x00")
            or reader.casefold() == control.casefold()
        ):
            raise SqlServerError(
                "Use distinct source reader and control principal identities."
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
def connection(values, role="source"):
    """Use encrypted process-local FreeTDS settings and role-specific auth.

    Certificate trust is verified by default. Explicit private PoC trust=true
    retains encryption but skips chain/name validation; it is never inferred
    from connection failures. The source role always requests read_only=True.
    All mutations remain uncommitted until the caller explicitly commits.
    """
    selected = validate_settings(values, role=role)
    prefix = "SOURCE_SQLSERVER"
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


def ownership(cursor, values, role="source"):
    """Compare the connected database and immutable marker in constant time."""
    if role not in {"source", "control"}:
        raise SqlServerError("Invalid fixed SQL operation role.")
    cursor.execute("SELECT DB_NAME()")
    row = cursor.fetchone()
    if not row or row[0] != values["SOURCE_SQLSERVER_DATABASE"]:
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


def side_effects_safe(cursor, role="control"):
    """Trust only the exact DBA-installed, read-only dbo metadata checker.

    Caller metadata can omit inbound legacy FKs. Never fall back to the
    caller's catalog views if this checker is missing, changed or fails.
    """
    if role == "source":
        return True
    if role != "control":
        return False
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
        or row[0].replace("\r\n", "\n")
        not in (SAFETY_DEFINITION, CONTROL_SAFETY_DEFINITION)
    ):
        return False
    cursor.execute("EXEC [lh_demo_ctl].[check_write_safety_v1]", ())
    return cursor.fetchone() == (1,) and cursor.fetchone() is None


def source_permissions_safe(cursor):
    """Check only the fixed reader boundary, not unrelated DB objects.

    The caller has already rejected broad DB/server roles and capabilities.
    This is not proof of database-wide least privilege; DBA audit is separate.
    """
    cursor.execute("SELECT IS_MEMBER(N'lh_demo_reader')")
    if cursor.fetchone() != (1,):
        return False
    for schema in SCHEMAS.values():
        for operation in ("ALTER", "CONTROL", "TAKE OWNERSHIP"):
            cursor.execute(
                "SELECT HAS_PERMS_BY_NAME(%s,N'SCHEMA',%s)",
                (schema, operation),
            )
            if cursor.fetchone() != (0,):
                return False
    objects = ["lh_demo_ctl.installation"] + [
        item["schema"] + "." + item["table"] for item in TABLES.values()
    ]
    for name in objects:
        cursor.execute(
            "SELECT HAS_PERMS_BY_NAME(%s,N'OBJECT',N'SELECT')", (name,)
        )
        if cursor.fetchone() != (1,):
            return False
        # Effective rows include column-only grants and role inheritance.
        cursor.execute(
            "SELECT permission_name FROM sys.fn_my_permissions(%s,N'OBJECT')",
            (name,),
        )
        permissions = cursor.fetchall()
        if not permissions or any(
            row[0] is None
            or row[0]
            in {
                "INSERT",
                "UPDATE",
                "DELETE",
                "ALTER",
                "CONTROL",
                "TAKE OWNERSHIP",
            }
            for row in permissions
        ):
            return False
        # TOP (0) validates object/column SELECT without disclosing data.
        qualified = ".".join("[" + part + "]" for part in name.split("."))
        cursor.execute("SELECT TOP (0) * FROM " + qualified)
        cursor.fetchall()
    return True


def permissions_safe(cursor, role="source", *, full_audit=False):
    """読取とwatermark更新を分離し、過大権限は拒否する。"""
    if role not in {"source", "control"}:
        return False
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
    if role == "source" and not full_audit:
        return source_permissions_safe(cursor)
    watermarks = ["lh_demo_ctl.batch_watermarks"] if role == "control" else []
    allowed_by_operation = {
        "SELECT": ["lh_demo_ctl.installation", *watermarks],
        "INSERT": watermarks,
        "UPDATE": watermarks,
        "DELETE": [],
        "EXECUTE": [SAFETY_CHECKER] if role == "control" else [],
        "RECEIVE": [],
        "ALTER": [],
        "CONTROL": [],
        "TAKE OWNERSHIP": [],
    }
    if role == "source":
        allowed_by_operation.pop("SELECT")
    known_types = ",".join(
        "'" + kind + "'" for kind in PERMISSION_OBJECT_TYPES
    )
    child_types = ",".join(
        "'" + kind + "'" for kind in NON_GRANTABLE_CHILD_TYPES
    )
    eligible = (
        "o.is_ms_shipped=0 AND NOT (COALESCE(o.parent_object_id,0)>0 "
        "AND COALESCE(o.type,'') IN (" + child_types + "))"
    )
    # A future or unsupported securable must not fall outside a finite audit
    # list and silently pass. Refuse it without trying inapplicable grants.
    cursor.execute(
        "SELECT TOP (1) o.object_id FROM sys.objects o WHERE "
        + eligible
        + " AND (o.type IS NULL OR o.parent_object_id IS NULL "
        "OR o.type NOT IN (" + known_types + "))"
    )
    if cursor.fetchone():
        return False
    for operation, allowed in allowed_by_operation.items():
        # Calling HAS_PERMS_BY_NAME with a guessed permission/object pairing
        # can produce NULL for an unsupported permission, or miss CLR/synonym
        # grants when the type list is incomplete. Audit effective rows,
        # including column grants. OUTER APPLY also retains an empty/failed
        # lookup as NULL, so incomplete metadata cannot silently pass.
        clause = "p.permission_name IS NULL OR (p.permission_name=%s"
        arguments = [operation]
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
            "sys.schemas s ON o.schema_id=s.schema_id OUTER APPLY "
            "sys.fn_my_permissions(QUOTENAME(s.name)+N'.'+"
            "QUOTENAME(o.name),N'OBJECT') p WHERE "
            + eligible
            + " AND o.type IN ("
            + known_types
            + ") AND ("
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
    if role == "source":
        required.extend(
            (item["schema"] + "." + item["table"], "SELECT")
            for item in TABLES.values()
        )
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


def diagnose(values, *, role="source"):
    """SOURCE資格情報のみで固定6表の読み取り状態を返す。"""
    if role != "source":
        raise SqlServerError("Invalid SQL source diagnostic role.")
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
        selected = validate_settings(values, role=role)
        with connection(selected, role=role) as handle:
            result["connected"] = True
            cursor = handle.cursor()
            try:
                result["schema_version"] = ownership(
                    cursor, selected, role=role
                )
                result["ownership"] = True
            except SqlServerError:
                return result
            result["permissions_safe"] = permissions_safe(cursor, role=role)
            if not result["permissions_safe"]:
                return result
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
                # Capture metadata may be unavailable to the narrow reader.
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
