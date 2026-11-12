#!/usr/bin/env python3
"""Exercise scoped reader policy on the disposable CI loopback SQL server.

Fresh synthetic database and login only. No user configuration is consumed.
The enclosing CI job disposes its dedicated container, including these objects.
"""

import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import sqlserver_demo as adapter  # noqa: E402


def progress(stage):
    """Emit only fixture-owned stage labels, never SQL or values."""
    print("SCOPED_READER_STAGE=" + stage, flush=True)


def cdc_preflight_cases(cursor, login, password, suffix, receipts):
    """Run the production preflight against real generated CDC objects."""
    from hashlib import sha256

    sys.path.insert(0, str(ROOT / "infra" / "cdc"))
    import fixed_source

    database = "scoped_cdc_" + suffix
    cursor.execute("USE master")
    progress("create_synthetic_database")
    cursor.execute("CREATE DATABASE [" + database + "]")
    cursor.execute("USE [" + database + "]")
    cursor.execute("CREATE USER [reader] FOR LOGIN [" + login + "]")
    progress("create_fixed_schemas")
    for schema in adapter.SCHEMAS.values():
        cursor.execute("CREATE SCHEMA [" + schema + "] AUTHORIZATION dbo")
    identity = secrets.token_hex(16)
    token = secrets.token_hex(32)
    cursor.execute(
        "CREATE TABLE lh_demo_ctl.installation ("
        "singleton_id int PRIMARY KEY, schema_version int,"
        "installation_id char(32), ownership_hash char(64))"
    )
    cursor.execute(
        "INSERT lh_demo_ctl.installation VALUES (1,1,%s,%s)",
        (identity, sha256(token.encode("ascii")).hexdigest()),
    )
    cursor.execute("GRANT SELECT ON lh_demo_ctl.installation TO reader")
    for role in ("lh_demo_reader", "lh_demo_cdc_reader"):
        cursor.execute("CREATE ROLE [" + role + "] AUTHORIZATION dbo")
        cursor.execute("ALTER ROLE [" + role + "] ADD MEMBER reader")
    progress("enable_synthetic_cdc_database")
    cursor.execute("EXEC sys.sp_cdc_enable_db")
    progress("create_six_synthetic_captures")
    for key, item in adapter.TABLES.items():
        qualified = "[" + item["schema"] + "].[" + item["table"] + "]"
        timestamps = {"updated_at", "created_at"} | (
            set(item["columns"]) & adapter.TIME_COLUMNS
        )
        definitions = []
        for column in [*item["columns"], *fixed_source.EXTRA_COLUMNS]:
            data_type = (
                "datetime2(7)" if column in timestamps else "nvarchar(128)"
            )
            if column == item["pk"]:
                data_type += " NOT NULL PRIMARY KEY"
            definitions.append("[" + column + "] " + data_type)
        cursor.execute(
            "CREATE TABLE " + qualified + " (" + ",".join(definitions) + ")"
        )
        cursor.execute("GRANT SELECT ON " + qualified + " TO reader")
        capture = adapter.CAPTURE_INSTANCES[key]
        cursor.execute(
            "EXEC sys.sp_cdc_enable_table @source_schema=%s,"
            "@source_name=%s,@capture_instance=%s,"
            "@role_name=N'lh_demo_cdc_reader',@supports_net_changes=0",
            (item["schema"], item["table"], capture),
        )
        for object_name in (
            capture + "_CT",
            "fn_cdc_get_all_changes_" + capture,
        ):
            cursor.execute(
                "GRANT SELECT ON [cdc].[" + object_name + "] TO reader"
            )
    for metadata in ("change_tables", "captured_columns", "lsn_time_mapping"):
        cursor.execute("GRANT SELECT ON [cdc].[" + metadata + "] TO reader")
    cursor.execute("GRANT VIEW DATABASE STATE TO reader")
    values = {
        "SOURCE_SQLSERVER_HOST": "127.0.0.1",
        "SOURCE_SQLSERVER_PORT": "14333",
        "SOURCE_SQLSERVER_DATABASE": database,
        "SOURCE_SQLSERVER_USER": login,
        "SOURCE_SQLSERVER_PASSWORD": password,
        "SOURCE_SQLSERVER_TRUST_SERVER_CERTIFICATE": "true",
        "DEMO_SQLSERVER_INSTALLATION_ID": identity,
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": token,
    }

    def check(case, expected):
        progress(case)
        try:
            result = fixed_source.preflight(
                values, fixed_source.table_mapping()
            )
        except ValueError:
            actual = False
        else:
            actual = result["checked"] and result["tables"] == 6
            if result["agent_jobs_checked"] is not False:
                raise RuntimeError("AGENT_RECEIPT_MUST_STAY_UNCHECKED")
        if actual is not expected:
            raise RuntimeError("CDC_READER_CASE_FAILED:" + case)
        receipts.append({"case": case, "passed": True})

    def mutation(case, change, restore):
        cursor.execute(change)
        try:
            check(case, False)
        finally:
            cursor.execute(restore)
        check(case + "_restored", True)

    # This executes the actual CT and generated TVF TOP(0), including the
    # NULL LSN parameters. An optimizer/permission error must fail this case.
    check("cdc_real_six_capture_preflight", True)
    for role in ("lh_demo_reader", "lh_demo_cdc_reader"):
        mutation(
            "cdc_missing_" + role,
            "ALTER ROLE " + role + " DROP MEMBER reader",
            "ALTER ROLE " + role + " ADD MEMBER reader",
        )
    mutation(
        "cdc_missing_database_state",
        "REVOKE VIEW DATABASE STATE FROM reader",
        "GRANT VIEW DATABASE STATE TO reader",
    )
    capture = next(iter(adapter.CAPTURE_INSTANCES.values()))
    for name, label in (
        ("change_tables", "metadata"),
        (capture + "_CT", "change_table"),
        ("fn_cdc_get_all_changes_" + capture, "generated_function"),
    ):
        mutation(
            "cdc_missing_" + label + "_select",
            "DENY SELECT ON [cdc].[" + name + "] TO reader",
            "GRANT SELECT ON [cdc].[" + name + "] TO reader",
        )


def main():
    """Keep all endpoints fixed and test real SQL effective permissions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("DISPOSABLE_CI_ONLY")
    import pymssql

    admin_password = os.environ["LAKEHOUSE_FIXTURE_PASSWORD"]
    suffix = secrets.token_hex(6)
    database = "scoped_reader_" + suffix
    login = "reader_fixture_" + suffix
    password = secrets.token_hex(24) + "Aa1!"
    progress("admin_connect")
    admin = pymssql.connect(
        server="127.0.0.1",
        port=14333,
        user="sa",
        password=admin_password,
        database="master",
        autocommit=True,
        login_timeout=10,
        timeout=30,
    )
    progress("server_version")
    cursor = admin.cursor()
    cursor.execute(
        "SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ProductMajorVersion')),"
        "CONVERT(nvarchar(128),SERVERPROPERTY('Edition'))"
    )
    version, edition = cursor.fetchone()
    if str(version) != "16" or "Developer" not in edition:
        raise RuntimeError("SQL2022_DEVELOPER_REQUIRED")
    progress("create_synthetic_database")
    cursor.execute("CREATE DATABASE [" + database + "]")
    progress("create_synthetic_login")
    cursor.execute(
        "CREATE LOGIN [" + login + "] WITH PASSWORD=%s, CHECK_POLICY=OFF",
        (password,),
    )
    cursor.execute("USE [" + database + "]")
    cursor.execute("CREATE USER [reader] FOR LOGIN [" + login + "]")
    progress("create_fixed_schemas")
    for schema in adapter.SCHEMAS.values():
        cursor.execute("CREATE SCHEMA [" + schema + "] AUTHORIZATION dbo")
    cursor.execute("CREATE ROLE [lh_demo_reader] AUTHORIZATION dbo")
    cursor.execute("ALTER ROLE [lh_demo_reader] ADD MEMBER [reader]")
    objects = ["lh_demo_ctl.installation"] + [
        item["schema"] + "." + item["table"]
        for item in adapter.TABLES.values()
    ]
    for name in objects:
        qualified = ".".join("[" + part + "]" for part in name.split("."))
        cursor.execute("CREATE TABLE " + qualified + " (id int, value int)")
        cursor.execute("GRANT SELECT ON OBJECT::" + qualified + " TO reader")
    cursor.execute("CREATE SCHEMA [unrelated] AUTHORIZATION dbo")
    cursor.execute("CREATE TABLE unrelated.legacy (id int)")
    # There are deliberately no permissions on the unrelated object.
    receipts = []

    def connect():
        return pymssql.connect(
            server="127.0.0.1",
            port=14333,
            user=login,
            password=password,
            database=database,
            autocommit=True,
            login_timeout=10,
            timeout=30,
        )

    def check(case, expected):
        progress(case)
        with connect() as reader:
            actual = adapter.permissions_safe(reader.cursor(), role="source")
        if actual is not expected:
            raise RuntimeError("SCOPED_READER_CASE_FAILED:" + case)
        receipts.append({"case": case, "passed": True})

    progress("unrelated_zero_rows_probe")
    with connect() as reader:
        probe = reader.cursor()
        probe.execute(
            "SELECT permission_name FROM "
            "sys.fn_my_permissions(N'unrelated.legacy',N'OBJECT')"
        )
        if probe.fetchall():
            raise RuntimeError("UNRELATED_ZERO_ROWS_REQUIRED")
    check("unrelated_zero_permissions", True)
    # An unrelated write grant is outside this source startup boundary.
    # It remains the DBA's responsibility; writer/control must still reject it.
    cursor.execute("GRANT INSERT ON unrelated.legacy TO reader")
    check("unrelated_write_is_not_startup_audit", True)
    for role in ("demo", "control"):
        with connect() as reader:
            if adapter.permissions_safe(reader.cursor(), role=role):
                raise RuntimeError("WRITER_CONTROL_AUDIT_WEAKENED")
        receipts.append({"case": role + "_audit_retained", "passed": True})
    cursor.execute("REVOKE INSERT ON unrelated.legacy FROM reader")

    def mutation(case, change, restore):
        cursor.execute(change)
        try:
            check(case, False)
        finally:
            cursor.execute(restore)
        check(case + "_restored", True)

    mutation(
        "missing_reader_role",
        "ALTER ROLE lh_demo_reader DROP MEMBER reader",
        "ALTER ROLE lh_demo_reader ADD MEMBER reader",
    )
    mutation(
        "missing_fixed_select",
        "REVOKE SELECT ON lh_demo_erp.products FROM reader",
        "GRANT SELECT ON lh_demo_erp.products TO reader",
    )
    for schema in adapter.SCHEMAS.values():
        for permission in ("ALTER", "CONTROL", "TAKE OWNERSHIP"):
            mutation(
                "schema_" + schema + "_" + permission.replace(" ", "_"),
                "GRANT " + permission + " ON SCHEMA::" + schema + " TO reader",
                "REVOKE "
                + permission
                + " ON SCHEMA::"
                + schema
                + " FROM reader",
            )
    for permission in ("INSERT", "UPDATE", "DELETE", "ALTER", "CONTROL"):
        mutation(
            "fixed_table_" + permission,
            "GRANT " + permission + " ON lh_demo_erp.products TO reader",
            "REVOKE " + permission + " ON lh_demo_erp.products FROM reader",
        )
    cursor.execute("DENY UPDATE ON lh_demo_erp.products TO reader")
    cursor.execute("GRANT UPDATE (value) ON lh_demo_erp.products TO reader")
    try:
        check("column_update_overrides_object_deny", False)
    finally:
        cursor.execute(
            "REVOKE UPDATE (value) ON lh_demo_erp.products FROM reader"
        )
        cursor.execute("REVOKE UPDATE ON lh_demo_erp.products FROM reader")
    check("column_update_restored", True)
    mutation(
        "broad_db_role",
        "ALTER ROLE db_datawriter ADD MEMBER reader",
        "ALTER ROLE db_datawriter DROP MEMBER reader",
    )
    cdc_preflight_cases(cursor, login, password, suffix, receipts)
    admin.close()
    print("SCOPED_READER_RECEIPT=" + json.dumps(receipts, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Only type, numeric SQL code and this public fixture's line number.
        print(
            "SCOPED_READER_EXCEPTION_TYPE=" + type(error).__name__,
            file=sys.stderr,
        )
        if error.args and type(error.args[0]) is int:
            print(
                "SCOPED_READER_SQL_NUMBER=" + str(error.args[0]),
                file=sys.stderr,
            )
        trace = error.__traceback__
        line = None
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__:
                line = trace.tb_lineno
            trace = trace.tb_next
        if line is not None:
            print("SCOPED_READER_FIXTURE_LINE=" + str(line), file=sys.stderr)
        # Never print raw driver diagnostics or generated credentials.
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print("SCOPED_READER_FIXTURE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
