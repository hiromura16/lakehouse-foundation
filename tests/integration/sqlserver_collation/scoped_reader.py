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
    cursor = admin.cursor()
    cursor.execute(
        "SELECT SERVERPROPERTY('ProductMajorVersion'),"
        "SERVERPROPERTY('Edition')"
    )
    version, edition = cursor.fetchone()
    if str(version) != "16" or "Developer" not in edition:
        raise RuntimeError("SQL2022_DEVELOPER_REQUIRED")
    cursor.execute("CREATE DATABASE [" + database + "]")
    cursor.execute(
        "CREATE LOGIN [" + login + "] WITH PASSWORD=%s, CHECK_POLICY=OFF",
        (password,),
    )
    cursor.execute("USE [" + database + "]")
    cursor.execute("CREATE USER [reader] FOR LOGIN [" + login + "]")
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
        with connect() as reader:
            actual = adapter.permissions_safe(reader.cursor(), role="source")
        if actual is not expected:
            raise RuntimeError("SCOPED_READER_CASE_FAILED:" + case)
        receipts.append({"case": case, "passed": True})

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
    admin.close()
    print("SCOPED_READER_RECEIPT=" + json.dumps(receipts, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print raw driver diagnostics or generated credentials.
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        else:
            print("SCOPED_READER_FIXTURE_FAILED", file=sys.stderr)
        raise SystemExit(1) from None
