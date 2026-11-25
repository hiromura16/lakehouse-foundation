"""使い捨てSQL Serverの固定DML入口で、書込の許可と拒否を実行する。"""

import json
import os
import secrets
import sys
from pathlib import Path

from run import render, variables

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
import sqlserver_batch as batch  # noqa: E402
import sqlserver_demo as adapter  # noqa: E402

HOST = "127.0.0.1"
PORT = 14333
WRITER = "fixture_writer"
PRODUCTS = "[lh_demo_erp].[products]"
GUARD_FAILURE = "Fixed demo writer permissions exceed the allowed boundary"


def require(condition):
    """失敗時は呼出側の固定stageだけを表示する。"""
    if not condition:
        raise RuntimeError("SYNTHETIC_WRITER_EVIDENCE_FAILED")


def stage(name):
    """資格情報、識別子、SQL、のぞいたレコードは出力しない。"""
    print("SCOPED_WRITER_STAGE=" + name, flush=True)


def fixture_database(cursor, database, logins, selected):
    """新規DBに公開DDLをそのまま適用し、無関係な別ユーザーも持つ。"""
    cursor.execute(
        "SELECT CONVERT(nvarchar(128),SERVERPROPERTY('ProductMajorVersion')),"
        "CONVERT(nvarchar(128),SERVERPROPERTY('Edition'))"
    )
    version, edition = cursor.fetchone()
    require(str(version) == "16" and "Developer" in edition)
    stage("create_disposable_database")
    cursor.execute("CREATE DATABASE [" + database + "]")
    accounts = (WRITER, "fixture_control")
    for login, password in logins:
        cursor.execute(
            "CREATE LOGIN [" + login + "] WITH PASSWORD=%s,CHECK_POLICY=OFF",
            (password,),
        )
    cursor.execute("USE [" + database + "]")
    for user, (login, _) in zip(accounts, logins, strict=True):
        cursor.execute("CREATE USER [" + user + "] FOR LOGIN [" + login + "]")
    for user in ("fixture_reader", "fixture_legacy"):
        cursor.execute("CREATE USER [" + user + "] WITHOUT LOGIN")
    for number in ("001", "002", "003"):
        stage("canonical_ddl_" + number)
        cursor.execute(render(number, selected))
        while cursor.nextset():
            pass
    stage("seed_disposable_legacy")
    cursor.execute("CREATE SCHEMA legacy AUTHORIZATION dbo")
    cursor.execute("CREATE TABLE legacy.unrelated (id int NOT NULL)")
    cursor.execute("INSERT INTO legacy.unrelated VALUES (42)")
    cursor.execute("CREATE ROLE fixture_legacy_role AUTHORIZATION dbo")
    cursor.execute(
        "GRANT SELECT ON OBJECT::legacy.unrelated TO fixture_legacy_role"
    )
    cursor.execute("ALTER ROLE fixture_legacy_role ADD MEMBER fixture_legacy")


def positive_permission_evidence(values):
    """実SQLで0件とOUTERの補完NULLを区別し、固定所有権も検証する。"""
    stage("normal_writer_login_and_role")
    with adapter.connection(values) as writer:
        cursor = writer.cursor()
        cursor.execute(
            "SELECT IS_MEMBER(N'lh_demo_writer'), "
            "IS_MEMBER(N'fixture_legacy_role')"
        )
        require(cursor.fetchone() == (1, 0))
        # 対象名を固定した一行で、権限TVFの実出力は0件、従来の外部
        # 結合はNULL行1件になる。別objectのカタログ可視性は仮定しない。
        stage("normal_writer_sql_apply_zero_rows")
        for apply, expected in (("CROSS APPLY", 0), ("OUTER APPLY", 1)):
            cursor.execute(
                "SELECT COUNT_BIG(*) FROM "
                "(VALUES(N'legacy.unrelated')) AS q(name) "
                + apply
                + " sys.fn_my_permissions(q.name,N'OBJECT') p "
                "WHERE p.permission_name IS NULL"
            )
            require(cursor.fetchone() == (expected,))
        stage("normal_writer_runtime_allow")
        require(adapter.permissions_safe(cursor))


def apply_legacy_policy(cursor, selected):
    """公開のオプションDDLを使い、固定スキーマだけを準備する。"""
    source = (
        ROOT / "infra/sqlserver/optional/007_writer_legacy_policy_v2.sql"
    ).read_text(encoding="utf-8")
    for key in ("TargetDatabase", "InstallationId", "OwnershipHash"):
        source = source.replace("$(" + key + ")", selected[key])
    require("$(" not in source)
    cursor.execute(source)
    while cursor.nextset():
        pass


def policy_row(cursor, operation):
    """固定の合成表への1種類の許可だけをDBAコンテキストで記す。"""
    cursor.execute(
        "INSERT INTO [lh_demo_ctl].[writer_legacy_policy_v2] "
        "SELECT i.installation_id,u.sid,u.principal_id,t.object_id,"
        "s.schema_id,s.name,t.name,t.create_date,%s "
        "FROM lh_demo_ctl.installation i CROSS JOIN sys.database_principals u "
        "JOIN sys.tables t ON t.object_id=OBJECT_ID(N'legacy.approved',N'U') "
        "JOIN sys.schemas s ON s.schema_id=t.schema_id "
        "WHERE i.singleton_id=1 AND u.name=N'fixture_writer'",
        (operation,),
    )
    cursor.execute(
        "SELECT COUNT_BIG(*) FROM lh_demo_ctl.writer_legacy_policy_v2 "
        "WHERE operation=%s AND object_id=OBJECT_ID(N'legacy.approved',N'U')",
        (operation,),
    )
    require(cursor.fetchone() == (1,))


def actual_role_dml(values, cursor, selected, run, sentinel, expected):
    """継承した実DMLは明示承認後だけ、固定DMLの判定に影響しない。"""
    cursor.execute("CREATE ROLE fixture_dml AUTHORIZATION dbo")
    cursor.execute("CREATE ROLE fixture_dml_nested AUTHORIZATION dbo")
    cursor.execute("ALTER ROLE fixture_dml ADD MEMBER fixture_dml_nested")
    cursor.execute("ALTER ROLE fixture_dml_nested ADD MEMBER fixture_writer")
    cursor.execute("CREATE TABLE legacy.approved (id int NOT NULL)")
    cursor.execute("INSERT INTO legacy.approved VALUES (37)")
    stage("unrelated_limited_role_alone")
    with adapter.connection(values) as writer:
        limited = writer.cursor()
        limited.execute("SELECT IS_MEMBER(N'fixture_dml')")
        require(limited.fetchone() == (1,))
    require(
        adapter.mutate(values, "insert", run_id=run, consent=True)[
            "changed_rows"
        ]
        == 1
    )
    compare_product(cursor, run, "合成CDC投入")
    require(
        adapter.mutate(values, "delete", run_id=run, consent=True)[
            "changed_rows"
        ]
        == 1
    )
    compare_product(cursor, run, None)
    compare_unrelated(cursor, sentinel, expected)
    cursor.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON legacy.approved "
        "TO fixture_dml"
    )
    with adapter.connection(values) as writer:
        limited = writer.cursor()
        limited.execute(
            "SELECT IS_MEMBER(N'lh_demo_writer'),IS_MEMBER(N'fixture_dml'), "
            "HAS_PERMS_BY_NAME(N'legacy.approved',N'OBJECT',N'SELECT'), "
            "HAS_PERMS_BY_NAME(N'legacy.approved',N'OBJECT',N'INSERT'), "
            "HAS_PERMS_BY_NAME(N'legacy.approved',N'OBJECT',N'UPDATE'), "
            "HAS_PERMS_BY_NAME(N'legacy.approved',N'OBJECT',N'DELETE')"
        )
        require(limited.fetchone() == (1, 1, 1, 1, 1, 1))

    def denied():
        """別の障害や試験の未実行を、権限拒否に数えない。"""
        try:
            adapter.mutate(values, "insert", run_id=run, consent=True)
        except adapter.SqlServerError as error:
            require(str(error).startswith(GUARD_FAILURE))
        else:
            raise RuntimeError("UNSAFE_DML_ACCEPTED")
        compare_product(cursor, run, None)
        compare_unrelated(cursor, sentinel, expected)
        cursor.execute("SELECT id FROM legacy.approved ORDER BY id")
        require(cursor.fetchall() == [(37,)])

    stage("inherited_dml_without_policy_refused")
    denied()
    stage("provision_read_only_policy")
    apply_legacy_policy(cursor, selected)
    policy_row(cursor, "INSERT")
    stage("wrong_policy_operation_refused")
    denied()
    for operation in ("SELECT", "UPDATE", "DELETE"):
        policy_row(cursor, operation)

    stage("exact_inherited_dml_approved")
    with adapter.connection(values) as writer:
        limited = writer.cursor()
        require(adapter.writer_permissions_safe(limited, values))
        require(not adapter.permissions_safe(limited, full_audit=True))
    require(
        adapter.mutate(values, "insert", run_id=run, consent=True)[
            "changed_rows"
        ]
        == 1
    )
    compare_product(cursor, run, "合成CDC投入")
    require(
        adapter.mutate(values, "delete", run_id=run, consent=True)[
            "changed_rows"
        ]
        == 1
    )
    compare_product(cursor, run, None)
    compare_unrelated(cursor, sentinel, expected)

    cases = (
        (
            "unapproved_object_refused",
            "GRANT INSERT ON legacy.unrelated TO fixture_dml",
            "REVOKE INSERT ON legacy.unrelated FROM fixture_dml",
        ),
        (
            "unapproved_select_refused",
            "GRANT SELECT ON legacy.unrelated TO fixture_dml",
            "REVOKE SELECT ON legacy.unrelated FROM fixture_dml",
        ),
        (
            "broad_schema_write_refused",
            "GRANT UPDATE ON SCHEMA::legacy TO fixture_dml",
            "REVOKE UPDATE ON SCHEMA::legacy FROM fixture_dml",
        ),
        (
            "approved_object_delegation_refused",
            "GRANT INSERT ON legacy.approved TO fixture_writer "
            "WITH GRANT OPTION",
            "REVOKE INSERT ON legacy.approved FROM fixture_writer CASCADE",
        ),
        (
            "nested_role_delegation_refused",
            "GRANT INSERT ON legacy.approved TO fixture_dml_nested "
            "WITH GRANT OPTION",
            "REVOKE INSERT ON legacy.approved FROM fixture_dml_nested CASCADE",
        ),
        (
            "approved_table_and_explicit_role_column_refused",
            "GRANT UPDATE (id) ON legacy.approved TO fixture_dml_nested",
            "REVOKE UPDATE (id) ON legacy.approved FROM fixture_dml_nested",
        ),
        (
            "approved_column_only_refused",
            "REVOKE UPDATE ON legacy.approved FROM fixture_dml; "
            "GRANT UPDATE (id) ON legacy.approved TO fixture_dml",
            "REVOKE UPDATE (id) ON legacy.approved FROM fixture_dml; "
            "GRANT UPDATE ON legacy.approved TO fixture_dml",
        ),
        (
            "third_party_policy_editor_refused",
            "GRANT UPDATE (operation) ON lh_demo_ctl.writer_legacy_policy_v2 "
            "TO fixture_legacy_role",
            "REVOKE UPDATE (operation) ON lh_demo_ctl.writer_legacy_policy_v2 "
            "FROM fixture_legacy_role",
        ),
        (
            "implicit_policy_editor_refused",
            "ALTER ROLE db_datawriter ADD MEMBER fixture_legacy",
            "ALTER ROLE db_datawriter DROP MEMBER fixture_legacy",
        ),
        (
            "stale_policy_incarnation_refused",
            "UPDATE lh_demo_ctl.writer_legacy_policy_v2 SET "
            "object_created_at=DATEADD(second,-1,object_created_at)",
            "UPDATE lh_demo_ctl.writer_legacy_policy_v2 SET "
            "object_created_at=DATEADD(second,1,object_created_at)",
        ),
    )
    for name, mutate, rollback in cases:
        stage(name)
        cursor.execute(mutate)
        try:
            if name in (
                "approved_object_delegation_refused",
                "nested_role_delegation_refused",
            ):
                grantee = (
                    "fixture_writer"
                    if name == "approved_object_delegation_refused"
                    else "fixture_dml_nested"
                )
                cursor.execute(
                    "SELECT COUNT_BIG(*) FROM sys.database_permissions "
                    "WHERE major_id=OBJECT_ID(N'legacy.approved',N'U') "
                    "AND grantee_principal_id=DATABASE_PRINCIPAL_ID(%s) "
                    "AND state=N'W' AND permission_name=N'INSERT'",
                    (grantee,),
                )
                require(cursor.fetchone() == (1,))
            denied()
        finally:
            cursor.execute(rollback)
    for db_role in ("db_owner", "db_ddladmin"):
        stage("dangerous_role_" + db_role)
        cursor.execute("ALTER ROLE " + db_role + " ADD MEMBER fixture_writer")
        try:
            denied()
        finally:
            cursor.execute(
                "ALTER ROLE " + db_role + " DROP MEMBER fixture_writer"
            )
    stage("dangerous_role_sysadmin")
    login = "[" + values["DEMO_SQLSERVER_USER"] + "]"
    database = "[" + selected["TargetDatabase"] + "]"

    def server_role(change):
        """サーバーロールの試験は作成した合成loginだけ。"""
        cursor.execute("USE master")
        try:
            cursor.execute("ALTER SERVER ROLE [sysadmin] " + change + login)
        finally:
            cursor.execute("USE " + database)

    server_role("ADD MEMBER ")
    try:
        denied()
    finally:
        server_role("DROP MEMBER ")
    stage("exact_policy_recovered")
    require(
        adapter.mutate(values, "insert", run_id=run, consent=True)[
            "changed_rows"
        ]
        == 1
    )
    compare_product(cursor, run, "合成CDC投入")
    compare_unrelated(cursor, sentinel, expected)
    cursor.execute("SELECT id FROM legacy.approved ORDER BY id")
    require(cursor.fetchall() == [(37,)])


def metadata_only(values, cursor, run, sentinel, expected):
    """別objectのメタデータだけが見えても固定DMLは可能。"""
    stage("metadata_only_no_data_rights")
    cursor.execute(
        "GRANT VIEW DEFINITION ON legacy.unrelated TO fixture_writer"
    )
    try:
        with adapter.connection(values) as writer:
            limited = writer.cursor()
            limited.execute(
                "SELECT OBJECT_ID(N'legacy.unrelated',N'U'),"
                "HAS_PERMS_BY_NAME(N'legacy.unrelated',N'OBJECT',N'SELECT'),"
                "HAS_PERMS_BY_NAME(N'legacy.unrelated',N'OBJECT',N'INSERT'),"
                "HAS_PERMS_BY_NAME(N'legacy.unrelated',N'OBJECT',N'UPDATE'),"
                "HAS_PERMS_BY_NAME(N'legacy.unrelated',N'OBJECT',N'DELETE')"
            )
            seen = limited.fetchone()
            require(seen and type(seen[0]) is int and seen[0] > 0)
            require(seen[1:] == (0, 0, 0, 0))
        require(
            adapter.mutate(values, "insert", run_id=run, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        compare_product(cursor, run, "合成CDC投入")
        require(
            adapter.mutate(values, "delete", run_id=run, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        compare_product(cursor, run, None)
        compare_unrelated(cursor, sentinel, expected)
    finally:
        cursor.execute(
            "REVOKE VIEW DEFINITION ON legacy.unrelated FROM fixture_writer"
        )


def control_checks(values, cursor):
    """別の限定control接続の必須権限不足を実leaseで確認する。"""
    stage("control_rights_baseline")
    with batch.checkpoint_lease(values, adapter):
        pass
    cursor.execute(
        "DENY UPDATE ON lh_demo_ctl.batch_watermarks TO fixture_control"
    )
    try:
        stage("missing_control_rights_refused")
        # 実controlセッションで、接続不良ではなく特定権限の不足と
        # 既存の権限判定Falseを確かめてから、実際のlease拒否を確認する。
        with adapter.connection(values, role="control") as control:
            limited = control.cursor()
            adapter.ownership(limited, values, role="control")
            limited.execute(
                "SELECT HAS_PERMS_BY_NAME("
                "N'lh_demo_ctl.batch_watermarks',N'OBJECT',N'UPDATE')"
            )
            require(limited.fetchone() == (0,))
            require(not adapter.permissions_safe(limited, role="control"))
        entered = False
        try:
            with batch.checkpoint_lease(values, adapter):
                entered = True
        except adapter.SqlServerError as error:
            # connectionは内部BatchErrorを秘匿済みの外側例外に変換する。
            require(
                not entered
                and str(error)
                == "Fixed SQL operation failed; private details withheld."
            )
        else:
            raise RuntimeError("UNSAFE_CONTROL_LEASE_ACCEPTED")
    finally:
        cursor.execute(
            "REVOKE UPDATE ON lh_demo_ctl.batch_watermarks "
            "FROM fixture_control"
        )
    with batch.checkpoint_lease(values, adapter):
        pass


def check_status(values):
    """通常の限定writerでもCDC captureを参照せず診断が通る。"""
    stage("normal_writer_status")
    result = adapter.diagnose(values)
    require(
        result["connected"]
        and result["ownership"]
        and result["permissions_safe"]
        and result["schema_version"] == 1
        and len(result["tables"]) == 6
        and all(
            table["exists"] and table["capture_instance"] is None
            for table in result["tables"].values()
        )
    )


def compare_product(cursor, run, name):
    """別接続で固定行と原文の更新、または不在を確認する。"""
    key = "CDC-" + run
    cursor.execute(
        "SELECT product_name, run_id, original_values FROM "
        + PRODUCTS
        + " WHERE product_id=%s",
        (key,),
    )
    row = cursor.fetchone()
    if name is None:
        require(row is None)
    else:
        require(row is not None and row[:2] == (name, run))
        payload = json.loads(row[2])
        require(
            payload["product_id"] == key and payload["product_name"] == name
        )
    require(cursor.fetchone() is None)
    return row


def compare_unrelated(cursor, sentinel, expected):
    """別runの原文を含む固定行と、無関係な表の内容を維持する。"""
    require(compare_product(cursor, sentinel, "合成CDC投入") == expected)
    cursor.execute("SELECT id FROM legacy.unrelated ORDER BY id")
    require(cursor.fetchall() == [(42,)])


def mutations(values, cursor, run, sentinel, expected):
    """固定DMLが実際にcommitし、拒否ケースは1行も残らないこと。"""
    stage("fixed_dml_positive")
    for action, name in (
        ("insert", "合成CDC投入"),
        ("update", "合成CDC更新"),
        ("delete", None),
    ):
        require(
            adapter.mutate(values, action, run_id=run, consent=True)
            == {
                "action": action,
                "changed_rows": 1,
                "run_id": run,
                "source_only": True,
            }
        )
        compare_product(cursor, run, name)
        compare_unrelated(cursor, sentinel, expected)

    cases = (
        (
            "required_rights",
            "DENY UPDATE ON OBJECT::lh_demo_erp.products TO fixture_writer",
            "REVOKE UPDATE ON OBJECT::lh_demo_erp.products "
            "FROM fixture_writer",
        ),
        (
            "outside_insert",
            "GRANT INSERT ON OBJECT::legacy.unrelated TO fixture_writer",
            "REVOKE INSERT ON OBJECT::legacy.unrelated FROM fixture_writer",
        ),
        (
            "outside_column_update",
            "GRANT UPDATE (id) ON legacy.unrelated TO fixture_writer",
            "REVOKE UPDATE (id) ON legacy.unrelated FROM fixture_writer",
        ),
        (
            "broad_role",
            "ALTER ROLE db_datawriter ADD MEMBER fixture_writer",
            "ALTER ROLE db_datawriter DROP MEMBER fixture_writer",
        ),
        (
            "hidden_inbound_fk",
            "CREATE TABLE legacy.child ( product_id nvarchar(160) NOT NULL "
            "REFERENCES lh_demo_erp.products(product_id) ON DELETE CASCADE )",
            "DROP TABLE legacy.child",
        ),
    )
    for label, change, restore in cases:
        stage(label)
        cursor.execute(change)
        try:
            if label == "hidden_inbound_fk":
                with adapter.connection(values) as writer:
                    limited = writer.cursor()
                    limited.execute("SELECT OBJECT_ID(N'legacy.child',N'U')")
                    require(limited.fetchone() == (None,))
                    require(not adapter.side_effects_safe(limited))
            try:
                adapter.mutate(values, "insert", run_id=run, consent=True)
            except adapter.SqlServerError as error:
                require(str(error).startswith(GUARD_FAILURE))
            else:
                raise RuntimeError("UNSAFE_DML_ACCEPTED")
            compare_product(cursor, run, None)
            compare_unrelated(cursor, sentinel, expected)
        finally:
            cursor.execute(restore)
    stage("fixed_writer_restored")
    result = adapter.mutate(values, "insert", run_id=run, consent=True)
    require(result["changed_rows"] == 1)
    compare_product(cursor, run, "合成CDC投入")
    compare_unrelated(cursor, sentinel, expected)


def main():
    """同一リポジトリの使い捨てrunner以外は接続前に終了する。"""
    require(
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and os.environ.get("GITHUB_REPOSITORY")
        == "hiromura16/lakehouse-foundation"
        and os.environ.get("RUNNER_OS") == "Linux"
    )
    import pymssql

    suffix = secrets.token_hex(8)
    database = "lh_collation_" + suffix + "_writer"
    login = "writer_fixture_" + suffix
    password = secrets.token_hex(24) + "Aa1!"
    control_login = "control_fixture_" + suffix
    control_password = secrets.token_hex(24) + "Aa1!"
    nonce = secrets.token_hex(16)
    selected = variables(database, nonce, os.environ["GITHUB_SHA"])
    values = {
        "DEMO_SQLSERVER_HOST": HOST,
        "DEMO_SQLSERVER_PORT": str(PORT),
        "DEMO_SQLSERVER_DATABASE": database,
        "DEMO_SQLSERVER_USER": login,
        "DEMO_SQLSERVER_PASSWORD": password,
        "DEMO_SQLSERVER_TRUST_SERVER_CERTIFICATE": "false",
        "DEMO_SQLSERVER_CA_FILE": os.environ["LAKEHOUSE_FIXTURE_CA"],
        "DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "localhost",
        "DEMO_SQLSERVER_INSTALLATION_ID": nonce,
        "DEMO_SQLSERVER_OWNERSHIP_TOKEN": nonce * 2,
    }
    stage("prepare")
    with pymssql.connect(
        server=HOST,
        port=PORT,
        user="sa",
        password=os.environ["LAKEHOUSE_FIXTURE_PASSWORD"],
        database="master",
        autocommit=True,
        login_timeout=10,
        timeout=30,
    ) as admin:
        cursor = admin.cursor()
        fixture_database(
            cursor,
            database,
            ((login, password), (control_login, control_password)),
            selected,
        )
        stage("wrong_certificate_name_refused")
        wrong = {
            **values,
            "DEMO_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "wrong.example.invalid",
        }
        require(not adapter.diagnose(wrong)["connected"])
        stage("zero_effective_permissions")
        positive_permission_evidence(values)
        check_status(values)
        run = secrets.token_hex(6)
        sentinel = secrets.token_hex(6)
        require(run != sentinel)
        require(
            adapter.mutate(values, "insert", run_id=sentinel, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        expected = compare_product(cursor, sentinel, "合成CDC投入")
        compare_unrelated(cursor, sentinel, expected)
        metadata_only(values, cursor, run, sentinel, expected)
        control_checks(
            {
                **values,
                "SOURCE_SQLSERVER_HOST": HOST,
                "SOURCE_SQLSERVER_PORT": str(PORT),
                "SOURCE_SQLSERVER_DATABASE": database,
                "SOURCE_SQLSERVER_USER": "fixture_reader",
                "SOURCE_SQLSERVER_CONTROL_USER": control_login,
                "SOURCE_SQLSERVER_CONTROL_PASSWORD": control_password,
                "SOURCE_SQLSERVER_CA_FILE": values["DEMO_SQLSERVER_CA_FILE"],
                "SOURCE_SQLSERVER_HOSTNAME_IN_CERTIFICATE": "localhost",
            },
            cursor,
        )
        mutations(values, cursor, run, sentinel, expected)
        require(
            adapter.mutate(values, "delete", run_id=run, consent=True)[
                "changed_rows"
            ]
            == 1
        )
        actual_role_dml(values, cursor, selected, run, sentinel, expected)
    stage("passed")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("FAIL: SCOPED_WRITER_ACCEPTANCE", file=sys.stderr)
        raise SystemExit(1) from None
